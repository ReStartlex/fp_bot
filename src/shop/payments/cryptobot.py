"""
Crypto Pay API клиент (@CryptoBot).

Документация: https://help.crypt.bot/crypto-pay-api

Зачем именно CryptoBot:
  - комиссия ~3% (vs ~30% у Telegram Stars);
  - юзер платит криптой (USDT/TON/BTC/...) — мы получаем фиат (RUB);
  - выводы на крипто-кошельки / прямой обмен на TON для Stars-рефаундов;
  - не требует ИП/самозанятого, mass-market доступен.

Архитектура:
  1. Создание invoice: POST createInvoice → возвращает pay_url и invoice_id;
  2. Сохраняем ShopPayment(provider='cryptobot', provider_invoice_id=...);
  3. Юзер платит → CryptoBot шлёт webhook ИЛИ мы polling'уем getInvoices;
  4. На событие 'paid' — идемпотентно начисляем баланс (apply_paid_invoice).

Безопасность:
  - HMAC-SHA256 подпись webhook'а проверяется в verify_webhook_signature;
  - timeout на HTTP-запросы 10с (CryptoBot обычно отвечает <1с);
  - api_token хранится как SecretStr в Settings и НИКОГДА не логируется.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from loguru import logger


# Базовые URL для main-net и test-net (@CryptoTestnetBot).
CRYPTOBOT_MAINNET_URL = "https://pay.crypt.bot/api"
CRYPTOBOT_TESTNET_URL = "https://testnet-pay.crypt.bot/api"


class CryptoBotError(Exception):
    """Ошибка API CryptoBot (response.ok=false)."""

    def __init__(self, code: int | None, name: str, raw: dict | None = None) -> None:
        self.code = code
        self.name = name
        self.raw = raw or {}
        super().__init__(f"CryptoBot API error [{code}]: {name}")


@dataclass(frozen=True)
class Invoice:
    """
    Минимальная модель invoice'а из CryptoBot API.

    Все поля strict (никаких Optional там, где их быть не должно по контракту
    API) — если CryptoBot сломает контракт, мы упадём на парсинге, а не
    тихо примем половину данных.

    Документация всех полей:
    https://help.crypt.bot/crypto-pay-api#Invoice
    """
    invoice_id: int           # уникальный идентификатор у CryptoBot
    status: str               # "active" | "paid" | "expired"
    amount: Decimal           # сумма (в нашем случае — fiat RUB)
    fiat: str                 # "RUB" | "USD" | ... — fiat-код
    pay_url: str              # ссылка для оплаты (юзеру кидаем в bot)
    description: str | None   # описание (видит юзер на CryptoBot)
    payload: str | None       # наш произвольный payload (мы кладём payment_id)
    created_at: str | None    # ISO-timestamp
    paid_at: str | None       # ISO-timestamp оплаты; None если не оплачен
    raw: dict[str, Any]       # полный raw для аудита/отладки

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Invoice":
        # Crypto Pay в разных версиях API называет URL по-разному:
        # старое поле было 'pay_url', новое — 'bot_invoice_url' /
        # 'mini_app_invoice_url' / 'web_app_invoice_url'.
        # Берём первое доступное в порядке предпочтения.
        pay_url = (
            data.get("bot_invoice_url")
            or data.get("pay_url")
            or data.get("mini_app_invoice_url")
            or data.get("web_app_invoice_url")
            or ""
        )
        return cls(
            invoice_id=int(data["invoice_id"]),
            status=str(data["status"]),
            amount=Decimal(str(data.get("amount", "0"))),
            fiat=str(data.get("fiat") or data.get("asset") or ""),
            pay_url=pay_url,
            description=data.get("description"),
            payload=data.get("payload"),
            created_at=data.get("created_at"),
            paid_at=data.get("paid_at"),
            raw=data,
        )


class CryptoBotClient:
    """
    Тонкий async-клиент над Crypto Pay API.

    Использование:
        client = CryptoBotClient(api_token=..., testnet=False)
        invoice = await client.create_invoice(
            amount_rub=Decimal("500"),
            description="Пополнение NeuroDrop",
            payload="payment_id:42",
        )
        # ... юзер платит ...
        fresh = await client.get_invoice(invoice.invoice_id)
        if fresh.status == "paid":
            ...

    Один экземпляр клиента можно переиспользовать (httpx-клиент создаётся
    на каждый запрос для thread-safety; для high-load позже сделаем pool).
    """

    def __init__(
        self,
        *,
        api_token: str,
        testnet: bool = False,
        timeout: float = 10.0,
    ) -> None:
        if not api_token:
            raise ValueError("CryptoBot api_token обязателен")
        self._api_token = api_token
        self._base = CRYPTOBOT_TESTNET_URL if testnet else CRYPTOBOT_MAINNET_URL
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base

    # ─── public API ────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """Возвращает информацию о приложении (id, name). Удобно для health-check."""
        return await self._request("getMe")

    async def create_invoice(
        self,
        *,
        amount_rub: Decimal,
        description: str,
        payload: str,
        expires_in: int = 3600,
        allow_anonymous: bool = True,
    ) -> Invoice:
        """
        Создаёт инвойс на сумму в RUB. Юзер платит криптой по своему выбору,
        мы получаем RUB — конвертацию делает CryptoBot.

        payload: до 4096 символов произвольной строки. Мы кладём туда наш
        payment_id, чтобы при webhook/polling-событии быстро найти ShopPayment.

        expires_in: секунды до автоистечения инвойса (по умолчанию 1 час).
        Истекший invoice → status="expired"; юзер не сможет по нему заплатить.
        """
        if amount_rub <= 0:
            raise ValueError(f"amount_rub должен быть >0, получено {amount_rub}")
        body = {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": format(amount_rub, "f"),  # без 'e' и без trailing zeros
            "description": description,
            "payload": payload,
            "expires_in": int(expires_in),
            "allow_anonymous": allow_anonymous,
        }
        data = await self._request("createInvoice", body)
        return Invoice.from_api(data)

    async def get_invoices(
        self,
        *,
        status: str | None = None,
        invoice_ids: list[int] | None = None,
        offset: int = 0,
        count: int = 100,
    ) -> list[Invoice]:
        """
        Получить список invoice'ов (фильтр по status / список id).
        Используется в polling-воркере для проверки `status="paid"` за период.

        count ≤ 1000 по API; для polling каждые 30с count=100 хватает с запасом.
        """
        body: dict[str, Any] = {"offset": offset, "count": count}
        if status:
            body["status"] = status
        if invoice_ids:
            body["invoice_ids"] = ",".join(str(i) for i in invoice_ids)
        data = await self._request("getInvoices", body)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [Invoice.from_api(it) for it in items]

    async def get_invoice(self, invoice_id: int) -> Invoice | None:
        """Получить ОДИН invoice по id. None если не найден."""
        items = await self.get_invoices(invoice_ids=[invoice_id], count=1)
        return items[0] if items else None

    # ─── internals ──────────────────────────────────────────────────

    async def _request(
        self, method: str, body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base}/{method}"
        headers = {
            "Crypto-Pay-API-Token": self._api_token,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as cli:
            try:
                resp = await cli.post(url, json=body or {}, headers=headers)
            except httpx.HTTPError as exc:
                # Network error: connection refused, DNS, TLS… — мы это
                # потом обработаем в polling-воркере (retry с backoff).
                logger.warning(f"cryptobot {method} network error: {exc}")
                raise
        if resp.status_code >= 500:
            # 5xx — временная проблема на стороне CryptoBot. retry caller.
            logger.warning(
                f"cryptobot {method} HTTP {resp.status_code}: {resp.text[:200]}"
            )
            raise CryptoBotError(resp.status_code, "server_error",
                                 {"http_status": resp.status_code})
        try:
            data = resp.json()
        except ValueError:
            raise CryptoBotError(resp.status_code, "invalid_json",
                                 {"body": resp.text[:200]})
        if not data.get("ok"):
            err = data.get("error", {})
            raise CryptoBotError(err.get("code"), err.get("name", "unknown"), data)
        return data.get("result")


# ─── webhook signature verification ─────────────────────────────────


def verify_webhook_signature(
    *,
    api_token: str,
    raw_body: bytes,
    signature_hex: str,
) -> bool:
    """
    Проверяет подпись webhook'а от CryptoBot.

    Алгоритм (из официальной документации):
      secret = sha256(api_token)
      expected = hmac_sha256(secret, raw_body).hexdigest()

    Подпись передаётся в заголовке `crypto-pay-api-signature`.

    raw_body должен быть ТОТ ЖЕ байтовый поток, который пришёл в HTTP —
    не json.dumps(parsed) (иначе будут расхождения в whitespace/порядке полей).
    Поэтому в FastAPI берём `await request.body()`, а не Body(...).

    hmac.compare_digest — constant-time, защищает от timing-attack.
    """
    if not signature_hex:
        return False
    secret = hashlib.sha256(api_token.encode("utf-8")).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_hex.lower())
