"""Асинхронный клиент ns.gifts API v2 с HMAC-подписью."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
import pyotp
from loguru import logger

from src.config import Settings, get_settings
from src.ns.exceptions import (
    NSAPIError,
    NSAuthError,
    NSInsufficientFunds,
    NSOrderTimeoutError,
    from_status_code,
)
from src.ns.models import (
    BalanceResponse,
    CreateOrderResponse,
    ExchangeRateResponse,
    OrderInfo,
    OrderStatus,
    PayOrderResponse,
    StockResponse,
    TokenResponse,
)


class NSClient:
    """
    Асинхронный клиент NS.gifts.

    Использование:
        async with NSClient() as ns:
            balance = await ns.check_balance()
            stock = await ns.get_stock()
    """

    TOKEN_REFRESH_MARGIN_SECONDS = 600  # обновляем токен за 10 мин до истечения

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._auth_lock = asyncio.Lock()
        self._api_secret_bytes = base64.b64decode(
            self._settings.ns_api_secret.get_secret_value()
        )

    async def __aenter__(self) -> "NSClient":
        self._client = httpx.AsyncClient(
            base_url=self._settings.ns_base_url,
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "ns-funpay-bridge/0.1"},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "NSClient не инициализирован. Используй `async with NSClient() as ns:`"
            )
        return self._client

    @staticmethod
    def new_custom_id() -> str:
        """Сгенерировать новый UUID4 для custom_id."""
        return str(uuid.uuid4())

    def _sign(
        self,
        method: str,
        path: str,
        query: str,
        body: bytes,
        ts: str,
        token: str | None,
    ) -> str:
        """HMAC-SHA256 подпись согласно спецификации NS."""
        body_hash = hashlib.sha256(body or b"").hexdigest()
        parts = [method.upper(), path, query, ts]
        if token is not None:
            parts.append(token)
        parts.append(body_hash)
        string_to_sign = "\n".join(parts).encode()
        digest = hmac.new(self._api_secret_bytes, string_to_sign, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    async def login(self) -> str:
        """POST /api/v2/get_token. Возвращает свежий токен (TTL 2 часа)."""
        path = "/api/v2/get_token"
        body = json.dumps(
            {
                "login": self._settings.ns_login,
                "password": self._settings.ns_password.get_secret_value(),
            },
            separators=(",", ":"),
        ).encode()
        ts = str(int(time.time()))
        headers = {
            "X-User-Id": str(self._settings.ns_user_id),
            "X-Timestamp": ts,
            "X-Signature": self._sign("POST", path, "", body, ts, None),
            "Content-Type": "application/json",
        }
        logger.debug(f"NS login: POST {path}")
        try:
            r = await self.http.post(path, headers=headers, content=body)
        except httpx.HTTPError as exc:
            raise NSAPIError(0, f"Сеть упала на login: {exc}", path=path) from exc
        if r.status_code != 200:
            raise from_status_code(
                r.status_code, "login failed", response_body=r.text, path=path
            )
        data = TokenResponse.model_validate_json(r.content)
        self._token = data.token
        self._token_expires_at = time.time() + data.expires_in
        logger.info(f"NS login OK, токен живёт {data.expires_in} сек")
        return data.token

    async def _ensure_token(self) -> str:
        async with self._auth_lock:
            now = time.time()
            if self._token is None or (self._token_expires_at - now) < self.TOKEN_REFRESH_MARGIN_SECONDS:
                await self.login()
            return self._token  # type: ignore[return-value]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        with_totp: bool = False,
    ) -> Any:
        """Подписанный запрос с одним ретраем на 401 и общим retry-механизмом."""
        token = await self._ensure_token()
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        body: bytes
        if json_body is None:
            body = b""
        else:
            payload = dict(json_body)
            if with_totp and self._settings.ns_totp_secret:
                payload["totp_code"] = pyotp.TOTP(
                    self._settings.ns_totp_secret.get_secret_value()
                ).now()
            body = json.dumps(payload, separators=(",", ":")).encode()

        last_exc: Exception | None = None
        for attempt in range(1, self._settings.ns_retry_attempts + 1):
            ts = str(int(time.time()))
            headers = {
                "X-User-Id": str(self._settings.ns_user_id),
                "X-Timestamp": ts,
                "X-Token": token,
                "X-Signature": self._sign(method, path, query, body, ts, token),
                "Content-Type": "application/json",
            }
            url = path + (f"?{query}" if query else "")
            try:
                logger.debug(f"NS {method} {path} (attempt {attempt})")
                r = await self.http.request(method, url, headers=headers, content=body)
            except httpx.HTTPError as exc:
                last_exc = NSAPIError(0, f"Сеть упала: {exc}", path=path)
                if attempt < self._settings.ns_retry_attempts:
                    await asyncio.sleep(self._settings.ns_retry_delay_seconds)
                    continue
                raise last_exc from exc

            if r.status_code == 401 and attempt == 1:
                logger.warning("NS вернул 401, перелогиниваюсь и пробую снова")
                self._token = None
                token = await self._ensure_token()
                continue

            if 200 <= r.status_code < 300:
                if r.content:
                    return r.json()
                return None

            # Не-2xx, не-401 на первой попытке
            err = from_status_code(
                r.status_code,
                _short_message(r),
                response_body=r.text,
                path=path,
            )
            # 5xx ретраим, 4xx — нет
            if 500 <= r.status_code < 600 and attempt < self._settings.ns_retry_attempts:
                logger.warning(f"NS {r.status_code}, retry через {self._settings.ns_retry_delay_seconds}с")
                last_exc = err
                await asyncio.sleep(self._settings.ns_retry_delay_seconds)
                continue
            raise err
        # сюда не доходим, но на всякий
        assert last_exc is not None
        raise last_exc

    # ---------- Бизнес-методы ----------

    async def check_balance(self) -> BalanceResponse:
        data = await self._request("GET", "/api/v2/check_balance")
        return BalanceResponse.model_validate(data)

    async def get_stock(self) -> StockResponse:
        data = await self._request("GET", "/api/v2/stock")
        return StockResponse.model_validate(data)

    async def get_exchange_rate(self, service_id: int = 1) -> ExchangeRateResponse:
        data = await self._request(
            "POST", "/api/v2/exchange_rate", json_body={"service_id": service_id}
        )
        return ExchangeRateResponse.model_validate(data)

    async def create_order(
        self,
        service_id: int,
        fields: list[dict[str, Any]],
        custom_id: str | None = None,
    ) -> CreateOrderResponse:
        custom_id = custom_id or self.new_custom_id()
        data = await self._request(
            "POST",
            "/api/v2/create_order",
            json_body={
                "service_id": service_id,
                "custom_id": custom_id,
                "fields": fields,
            },
        )
        return CreateOrderResponse.model_validate(data)

    async def pay_order(self, custom_id: str) -> PayOrderResponse:
        if not self._settings.enable_real_actions:
            raise RuntimeError(
                "ENABLE_REAL_ACTIONS=false: реальная оплата заблокирована. "
                "Поставь в .env ENABLE_REAL_ACTIONS=true когда будешь готов."
            )
        data = await self._request(
            "POST",
            "/api/v2/pay_order",
            json_body={"custom_id": custom_id},
            with_totp=True,
        )
        resp = PayOrderResponse.model_validate(data)
        if resp.status == "insufficient":
            raise NSInsufficientFunds(custom_id=custom_id, balance=resp.balance)
        return resp

    async def order_info(self, custom_id: str) -> OrderInfo:
        data = await self._request("GET", f"/api/v2/order_info/{custom_id}")
        return OrderInfo.model_validate(data)

    async def wait_order_completion(self, custom_id: str) -> OrderInfo:
        """
        Опрашивает order_info пока заказ не дойдёт до финального статуса.
        Бросает NSOrderTimeoutError если не успел.
        """
        deadline = time.time() + self._settings.ns_order_timeout_seconds
        while time.time() < deadline:
            info = await self.order_info(custom_id)
            status = info.status_enum
            if status in (OrderStatus.COMPLETED, OrderStatus.REFUNDED, OrderStatus.CANCELLED):
                return info
            await asyncio.sleep(self._settings.ns_order_poll_interval_seconds)
        raise NSOrderTimeoutError(
            f"Заказ {custom_id} не завершился за "
            f"{self._settings.ns_order_timeout_seconds}с"
        )


def _short_message(r: httpx.Response) -> str:
    """Достать читаемый текст ошибки из ответа NS."""
    try:
        data = r.json()
        if isinstance(data, dict):
            return data.get("error") or data.get("message") or r.text[:200]
    except Exception:
        pass
    return r.text[:200]
