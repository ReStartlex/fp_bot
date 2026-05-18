"""Telegram-уведомления (через aiogram)."""
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from src.config import Settings, get_settings


class TelegramNotifier:
    """
    Минимальный нотификатор: шлёт сообщения в один чат.
    Безопасен в выключенном состоянии (telegram_enabled=false / нет токена).

    Использует raw httpx, чтобы не тянуть весь aiogram только ради sendMessage.
    Опционально использует прокси.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._client = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        s = self._settings
        return (
            s.telegram_enabled
            and s.telegram_bot_token is not None
            and s.telegram_chat_id is not None
        )

    async def __aenter__(self) -> "TelegramNotifier":
        if not self.enabled:
            return self
        import httpx

        proxy_url = self._settings.telegram_proxy_url
        if proxy_url:
            self._client = httpx.AsyncClient(proxy=proxy_url, timeout=15.0)
            logger.info(f"Telegram: использую прокси {self._settings.telegram_proxy_host}")
        else:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(
        self,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
    ) -> bool:
        """Отправить сообщение. Если выключено — silently no-op."""
        if not self.enabled or self._client is None:
            return False
        s = self._settings
        token = s.telegram_bot_token.get_secret_value()  # type: ignore[union-attr]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload: dict = {
            "chat_id": s.telegram_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        async with self._lock:
            try:
                r = await self._client.post(url, json=payload)
                if r.status_code != 200:
                    logger.warning(
                        f"Telegram sendMessage HTTP {r.status_code}: {r.text[:200]}"
                    )
                    return False
                return True
            except Exception as exc:
                logger.warning(
                    f"Telegram sendMessage failed: "
                    f"{type(exc).__name__}: {exc!r}"
                )
                return False

    # ----- Шорткаты для разных типов событий -----

    async def info(self, text: str) -> None:
        await self.send(f"ℹ️ {text}")

    async def warning(self, text: str) -> None:
        await self.send(f"⚠️ <b>WARN</b>: {text}")

    async def error(self, text: str) -> None:
        await self.send(f"🚨 <b>ERROR</b>: {text}")

    async def order_success(
        self,
        *,
        funpay_order_id: str,
        ns_custom_id: str,
        ns_price_usd: float,
        funpay_price_rub: Optional[float],
        buyer_username: Optional[str],
    ) -> None:
        text = (
            f"✅ <b>Заказ выполнен</b>\n"
            f"FunPay: <code>{funpay_order_id}</code>\n"
            f"NS: <code>{ns_custom_id}</code>\n"
            f"Куплено за: {ns_price_usd:.4f}$\n"
            f"Продано за: {funpay_price_rub or '?'}₽\n"
            f"Покупатель: {buyer_username or '?'}"
        )
        await self.send(text)

    async def order_failure(
        self,
        *,
        funpay_order_id: str,
        reason: str,
    ) -> None:
        text = (
            f"❌ <b>Заказ упал</b>\n"
            f"FunPay: <code>{funpay_order_id}</code>\n"
            f"Причина: <code>{reason[:300]}</code>"
        )
        await self.send(text)

    async def low_balance(self, *, current: float, threshold: float) -> None:
        text = (
            f"💸 <b>Баланс NS низкий</b>\n"
            f"Текущий: {current:.4f}$\n"
            f"Порог: {threshold:.2f}$"
        )
        await self.send(text)

    async def new_lot_discovered(
        self, funpay_lot_id: int, title: str | None
    ) -> None:
        """Уведомление о новом FunPay-лоте, у которого ещё нет маппинга."""
        from html import escape

        title_line = (
            f"\n<i>{escape(title)[:160]}</i>" if title else ""
        )
        text = (
            f"🆕 <b>Новый лот на FunPay</b>\n"
            f"ID: <code>{funpay_lot_id}</code>"
            f"{title_line}\n\n"
            f"Маппинга ещё нет — товар <b>не будет</b> выкупаться "
            f"автоматически, пока ты его не привяжешь к NS-сервису."
        )
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "🎯 Выбрать целью",
                        "callback_data": f"newlot:target:{funpay_lot_id}",
                    },
                ],
                [
                    {
                        "text": "🔬 Inspect",
                        "callback_data": f"newlot:inspect:{funpay_lot_id}",
                    },
                    {
                        "text": "✖ Скрыть",
                        "callback_data": "close",
                    },
                ],
            ]
        }
        await self.send(text, reply_markup=markup)
