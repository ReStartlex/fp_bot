"""
Обработка входящих сообщений в FunPay-чате.

Логика:
- На любое сообщение от покупателя обновляем last_seen и (если давно не здоровались)
  отвечаем приветствием с учётом рабочих часов.
- Если в сообщении есть help-триггер (!помощь, !help, !sos и т.д.) —
  отвечаем acknowledged-шаблоном и пингуем владельца в Telegram.
- Свои сообщения игнорируем.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from src.alerts.telegram import TelegramNotifier
from src.chat import templates
from src.chat.schedule import WorkingHours
from src.config import Settings, get_settings
from src.db.repo import (
    get_or_create_chat_state,
    mark_greeted,
    mark_help_requested,
)
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.funpay.events import FunPayMessageEvent


def _has_help_trigger(text: str, triggers: set[str]) -> bool:
    """True если в тексте сообщения есть хотя бы один help-токен."""
    if not text or not triggers:
        return False
    lowered = text.lower()
    return any(t in lowered for t in triggers)


def _shortlink(chat_id: int, username: str | None) -> str:
    """Удобная ссылка/упоминание для алерта в Telegram."""
    if username:
        return f"https://funpay.com/chat/?node={chat_id} (с @{username})"
    return f"https://funpay.com/chat/?node={chat_id}"


class ChatHandler:
    """Слой реакций на сообщения. Не имеет собственного жизненного цикла —
    вызывается из FunPay watcher на каждое новое сообщение."""

    def __init__(
        self,
        funpay_client: FunPayClient,
        telegram: Optional[TelegramNotifier] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._fp = funpay_client
        self._tg = telegram
        self._settings = settings or get_settings()
        self._wh = WorkingHours(
            start_hour=self._settings.work_hours_start,
            end_hour=self._settings.work_hours_end,
            tz_name=self._settings.timezone,
        )
        self._triggers = self._settings.help_trigger_set
        logger.info(
            f"ChatHandler init: триггеры={sorted(self._triggers)}, "
            f"рабочие часы={self._wh.format_window()} {self._wh.tz_name}, "
            f"автогритинг={'on' if self._settings.chat_autogreeting_enabled else 'off'}"
        )

    async def on_message(self, event: FunPayMessageEvent) -> None:
        if event.is_my_message:
            return
        text = (event.text or "").strip()
        if not text:
            return

        async with session_factory()() as session:
            state = await get_or_create_chat_state(
                session, chat_id=event.chat_id, buyer_username=event.author_username
            )
            await session.commit()

        if _has_help_trigger(text, self._triggers):
            await self._handle_help_request(event, state_chat_id=event.chat_id)
            return

        if self._settings.chat_autogreeting_enabled:
            await self._maybe_greet(event, state_chat_id=event.chat_id)

    # ---------- Сценарии ----------

    async def _handle_help_request(
        self, event: FunPayMessageEvent, state_chat_id: int
    ) -> None:
        working_now = self._wh.is_working_now()
        reply = templates.help_acknowledged(
            event.author_username, working_now=working_now, wh=self._wh
        )
        try:
            await self._fp.send_message(event.chat_id, reply)
        except Exception as exc:
            logger.warning(f"Не отправил help-ack в чат {event.chat_id}: {exc}")

        async with session_factory()() as session:
            state = await get_or_create_chat_state(
                session,
                chat_id=state_chat_id,
                buyer_username=event.author_username,
            )
            await mark_help_requested(session, state)
            await session.commit()

        # Telegram-алерт владельцу
        if self._tg is not None:
            urgency = "" if working_now else " 🌙 (вне рабочих часов!)"
            link = _shortlink(event.chat_id, event.author_username)
            await self._tg.send(
                f"🆘 <b>Покупатель просит помощь</b>{urgency}\n"
                f"От: @{event.author_username or '—'}\n"
                f"Сообщение: <code>{event.text[:300]}</code>\n"
                f"Чат: {link}"
            )

    async def _maybe_greet(
        self, event: FunPayMessageEvent, state_chat_id: int
    ) -> None:
        cooldown = timedelta(hours=self._settings.chat_greeting_cooldown_hours)

        async with session_factory()() as session:
            state = await get_or_create_chat_state(
                session,
                chat_id=state_chat_id,
                buyer_username=event.author_username,
            )
            if state.greeted_at is not None and datetime.utcnow() - state.greeted_at < cooldown:
                # Здоровались недавно — молчим.
                return
            await mark_greeted(session, state)
            await session.commit()

        working_now = self._wh.is_working_now()
        greeting = templates.greeting_pre_purchase(
            event.author_username, working_now=working_now, wh=self._wh
        )
        try:
            await self._fp.send_message(event.chat_id, greeting)
            logger.info(
                f"Поздоровался в чате {event.chat_id} с @{event.author_username}"
            )
        except Exception as exc:
            logger.warning(f"Не отправил приветствие в чат {event.chat_id}: {exc}")
