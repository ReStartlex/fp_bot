"""Telegram-уведомления (через aiogram)."""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from loguru import logger

from src.config import Settings, get_settings


def _is_network_error(exc: Exception) -> bool:
    """True если ошибка похожа на временную сетевую (retry имеет смысл)."""
    name = type(exc).__name__.lower()
    if any(token in name for token in ("timeout", "connect", "network", "readerror")):
        return True
    text = str(exc).lower()
    return any(token in text for token in ("timeout", "connection", "network"))


class TelegramNotifier:
    """
    Минимальный нотификатор: шлёт сообщения в один чат.
    Безопасен в выключенном состоянии (telegram_enabled=false / нет токена).

    Использует raw httpx, чтобы не тянуть весь aiogram только ради sendMessage.
    Опционально использует прокси с auto-fallback на direct: если прокси
    становится недоступен (TCP timeout, connect error и т.п.) — nотifier
    один раз пробует direct и, если direct работает, в этом процессе
    больше не пытается через прокси. Это защита от частого боевого кейса:
    aiogram-бот ходит к api.telegram.org напрямую и работает, а notifier
    с прокси из .env молча теряет все уведомления, потому что VPS не
    видит прокси (egress-блок).

    При временной недоступности Telegram (ConnectTimeout и т.п.):
      1) несколько быстрых retry с backoff;
      2) если не помогло — сообщение кладётся в SQLite-очередь
         pending_telegram_alerts и доставляется фоновым worker'ом.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._proxy_client = None
        self._direct_client = None
        self._proxy_dead = False
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
            self._proxy_client = httpx.AsyncClient(proxy=proxy_url, timeout=15.0)
            logger.info(
                f"Telegram: использую прокси {self._settings.telegram_proxy_host} "
                f"(с auto-fallback на direct при network error)"
            )
        else:
            self._direct_client = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for client_attr in ("_proxy_client", "_direct_client"):
            client = getattr(self, client_attr, None)
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass
                setattr(self, client_attr, None)

    def _active_client(self):
        """Какой клиент использовать прямо сейчас."""
        if self._proxy_dead or self._proxy_client is None:
            return self._direct_client
        return self._proxy_client

    async def _ensure_direct_client(self):
        """Лениво создать direct-клиент (только когда понадобится для fallback)."""
        if self._direct_client is None:
            import httpx

            self._direct_client = httpx.AsyncClient(timeout=15.0)
        return self._direct_client

    async def _post_once(
        self,
        url: str,
        payload: dict,
    ) -> tuple[bool, str | None]:
        """
        Одна попытка POST. Возвращает (success, error_reason).
        error_reason=None при success; иначе текст/тип ошибки.
        """
        async with self._lock:
            client = self._active_client()
            if client is None:
                return False, "no_http_client"
            via_proxy = (
                self._proxy_client is not None
                and not self._proxy_dead
                and client is self._proxy_client
            )
            try:
                r = await client.post(url, json=payload)
            except Exception as exc:
                if via_proxy:
                    logger.warning(
                        f"Telegram через прокси упал ({type(exc).__name__}: {exc!r}). "
                        "Пробую direct..."
                    )
                    ok = await self._try_direct_fallback(
                        url, payload, original_exc=exc
                    )
                    if ok:
                        return True, None
                    return False, f"{type(exc).__name__}: {exc!r}"
                return False, f"{type(exc).__name__}: {exc!r}"

            if r.status_code == 200:
                return True, None

            logger.warning(
                f"Telegram sendMessage HTTP {r.status_code}: {r.text[:200]}"
            )
            return False, f"HTTP {r.status_code}"

    async def send(
        self,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
        queue_on_failure: bool = True,
    ) -> bool:
        """Отправить сообщение. Если выключено — silently no-op."""
        if not self.enabled:
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

        max_retries = s.telegram_send_max_retries
        base_delay = s.telegram_send_retry_base_seconds
        last_error: str | None = None

        for attempt in range(max_retries):
            ok, err = await self._post_once(url, payload)
            if ok:
                return True
            last_error = err or "unknown"
            # HTTP-ошибки (400/403) не лечатся retry — не ставим в очередь.
            if err and err.startswith("HTTP "):
                return False
            if attempt + 1 < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    f"Telegram sendMessage retry {attempt + 1}/{max_retries} "
                    f"через {delay:.1f}s ({last_error})"
                )
                await asyncio.sleep(delay)

        logger.warning(
            f"Telegram sendMessage failed после {max_retries} попыток: {last_error}"
        )
        if queue_on_failure and last_error is not None:
            await self._enqueue_pending(text, parse_mode, reply_markup, last_error)
        return False

    async def _enqueue_pending(
        self,
        text: str,
        parse_mode: str,
        reply_markup: dict | None,
        error: str,
    ) -> None:
        try:
            from src.db.repo import enqueue_pending_telegram_alert
            from src.db.session import session_factory

            markup_json = json.dumps(reply_markup) if reply_markup else None
            async with session_factory()() as session:
                await enqueue_pending_telegram_alert(
                    session,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup_json=markup_json,
                    retry_after_seconds=self._settings.telegram_pending_retry_base_seconds,
                )
                await session.commit()
            logger.warning(
                f"Telegram: сообщение поставлено в очередь pending_alerts "
                f"(ошибка: {error[:120]})"
            )
        except Exception as exc:
            logger.warning(
                f"Telegram: не удалось поставить сообщение в очередь: {exc}"
            )

    async def flush_pending_alerts(self) -> dict[str, int]:
        """
        Повторная отправка due-сообщений из pending_telegram_alerts.
        Вызывается APScheduler-job'ом; не блокирует pipeline заказов.
        """
        if not self.enabled:
            return {"checked": 0, "sent": 0, "rescheduled": 0}

        from src.db.repo import (
            delete_pending_telegram_alert,
            list_due_pending_telegram_alerts,
            reschedule_pending_telegram_alert,
        )
        from src.db.session import session_factory

        sent = 0
        rescheduled = 0
        checked = 0

        try:
            async with session_factory()() as session:
                alerts = await list_due_pending_telegram_alerts(
                    session,
                    limit=self._settings.telegram_pending_max_per_flush,
                )
                checked = len(alerts)
                for alert in alerts:
                    markup = None
                    if alert.reply_markup_json:
                        try:
                            markup = json.loads(alert.reply_markup_json)
                        except json.JSONDecodeError:
                            markup = None
                    ok = await self.send(
                        alert.text,
                        parse_mode=alert.parse_mode,
                        reply_markup=markup,
                        queue_on_failure=False,
                    )
                    if ok:
                        await delete_pending_telegram_alert(session, alert.id)
                        sent += 1
                    else:
                        await reschedule_pending_telegram_alert(
                            session,
                            alert,
                            error=alert.last_error or "flush retry failed",
                            base_backoff_seconds=(
                                self._settings.telegram_pending_retry_base_seconds
                            ),
                        )
                        rescheduled += 1
                await session.commit()
        except Exception as exc:
            logger.warning(f"Telegram flush_pending_alerts упал: {exc}")

        if sent or rescheduled:
            logger.info(
                f"Telegram pending flush: checked={checked}, "
                f"sent={sent}, rescheduled={rescheduled}"
            )
        return {"checked": checked, "sent": sent, "rescheduled": rescheduled}

    async def _try_direct_fallback(
        self, url: str, payload: dict, *, original_exc: Exception
    ) -> bool:
        """Однократный fallback на direct после провала через прокси."""
        try:
            direct = await self._ensure_direct_client()
            r = await direct.post(url, json=payload)
        except Exception as fallback_exc:
            logger.warning(
                f"Telegram direct fallback тоже упал "
                f"({type(fallback_exc).__name__}: {fallback_exc!r}). "
                f"Изначальная ошибка прокси: "
                f"{type(original_exc).__name__}: {original_exc!r}"
            )
            return False
        if r.status_code == 200:
            self._proxy_dead = True
            logger.warning(
                f"Telegram-прокси {self._settings.telegram_proxy_host} "
                f"недоступен с этого хоста; direct работает. Дальше шлю "
                "напрямую без прокси (до рестарта процесса)."
            )
            return True
        logger.warning(
            f"Telegram direct fallback HTTP {r.status_code}: {r.text[:200]}"
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
        profit_rub: Optional[float] = None,
    ) -> None:
        profit_line = (
            f"Прибыль: {profit_rub:.2f}₽" if profit_rub is not None
            else "Прибыль: n/a"
        )
        text = (
            f"✅ <b>Заказ выполнен</b>\n"
            f"FunPay: <code>{funpay_order_id}</code>\n"
            f"NS: <code>{ns_custom_id}</code>\n"
            f"Куплено за: {ns_price_usd:.4f}$\n"
            f"Продано за: {funpay_price_rub or '?'}₽\n"
            f"{profit_line}\n"
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

    async def manual_hold_required(
        self,
        *,
        funpay_order_id: str,
        stage: str,
        age_seconds: int,
        buyer_username: str | None,
        ns_custom_id: str | None,
        has_pins: bool,
        reason: str,
    ) -> None:
        """
        Бросающийся в глаза алерт «бот не успел — выдай вручную».

        stage: словесная стадия pipeline, на которой выдохлись.
        age_seconds: сколько прошло от received до момента истечения.
        ns_custom_id: если уже создан заказ в NS — оператор по нему найдёт
            покупку в кабинете NS / в логах.
        has_pins: True если коды у нас уже на руках (pins_ready). Тогда
            оператор может просто нажать «🔁 Retry» и бот отправит сам.
        """
        from html import escape

        callback_safe_id = funpay_order_id.strip()
        if len(callback_safe_id) > 48:
            callback_safe_id = callback_safe_id[:48]
        age_minutes = max(1, round(age_seconds / 60))
        pins_line = (
            "✅ Коды NS уже на руках — Retry просто отправит их в чат."
            if has_pins
            else "⚠ Коды у NS ещё не получены — нужно выдать вручную или вернуть деньги."
        )
        ns_line = (
            f"NS: <code>{escape(ns_custom_id)}</code>\n" if ns_custom_id else ""
        )
        buyer_line = (
            f"Покупатель: {escape(buyer_username)}\n" if buyer_username else ""
        )
        text = (
            f"🛑 <b>РУЧНАЯ ВЫДАЧА</b>\n"
            f"FunPay: <code>{escape(funpay_order_id)}</code>\n"
            f"{buyer_line}"
            f"{ns_line}"
            f"Стадия: <code>{escape(stage)}</code>\n"
            f"Прошло: ~{age_minutes} мин\n\n"
            f"{pins_line}\n\n"
            f"Причина: <code>{escape(reason)[:240]}</code>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "🔁 Retry (force)",
                        "callback_data": f"hold:retry:{callback_safe_id}",
                    },
                    {
                        "text": "✅ Выдано вручную",
                        "callback_data": f"hold:done:{callback_safe_id}",
                    },
                ],
                [
                    {
                        "text": "ℹ️ Детали заказа",
                        "callback_data": f"hold:show:{callback_safe_id}",
                    },
                    {
                        "text": "✖ Скрыть",
                        "callback_data": "close",
                    },
                ],
            ]
        }
        await self.send(text, reply_markup=markup)

    async def new_lot_discovered(
        self, funpay_lot_id: int, title: str | None, *, suggestions=None
    ) -> None:
        """Уведомление о новом FunPay-лоте, у которого ещё нет маппинга."""
        from html import escape

        title_line = (
            f"\n<i>{escape(title)[:160]}</i>" if title else ""
        )
        suggestion_lines: list[str] = []
        for item in list(suggestions or [])[:3]:
            suggestion_lines.append(
                f"NS#{item.service_id} · {escape(item.service_name)[:80]} · "
                f"{item.price:.2f}{escape(item.currency)} · stock {item.in_stock}"
            )
        suggestions_text = ""
        if suggestion_lines:
            suggestions_text = (
                "\n\n<b>Возможные NS-услуги:</b>\n"
                + "\n".join(f"• {line}" for line in suggestion_lines)
            )
        text = (
            f"🆕 <b>Новый лот на FunPay</b>\n"
            f"ID: <code>{funpay_lot_id}</code>"
            f"{title_line}\n\n"
            f"Маппинга ещё нет — товар <b>не будет</b> выкупаться "
            f"автоматически, пока ты его не привяжешь к NS-сервису."
            f"{suggestions_text}"
        )
        suggestion_rows = [
            [
                {
                    "text": f"✅ NS#{item.service_id} · {item.service_name[:24]}",
                    "callback_data": f"newlot:map:{funpay_lot_id}:{item.service_id}",
                }
            ]
            for item in list(suggestions or [])[:3]
        ]
        markup = {
            "inline_keyboard": [
                *suggestion_rows,
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
