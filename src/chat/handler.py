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


_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\ufeff"


def _clean_chat_text(text: str) -> str:
    """FunPay иногда добавляет невидимые символы к эху наших сообщений."""
    cleaned = text
    for ch in _INVISIBLE_CHARS:
        cleaned = cleaned.replace(ch, "")
    return cleaned.strip()


def _has_help_trigger(text: str, triggers: set[str]) -> bool:
    """True если в тексте сообщения есть хотя бы один help-токен."""
    if not text or not triggers:
        return False
    lowered = _clean_chat_text(text).lower()
    return any(t in lowered for t in triggers)


def _looks_like_own_template_message(text: str) -> bool:
    """Fallback-фильтр для случаев, когда FunPay отдаёт наше сообщение как buyer."""
    cleaned = _clean_chat_text(text).lower()
    own_markers = (
        "готовлю ваш заказ",
        "спасибо за покупку",
        "ваш заказ готов",
        "пожалуйста, активируйте в течение 24",
        "уведомил продавца",
        "подключится к чату",
        "если всё хорошо, буду благодарен за отзыв",
        "выдача товара автоматическая",
        "автовыдача работает круглосуточно",
        "preparing your order",
        "your order is ready",
        "i've notified the seller",
        "delivery is automatic",
    )
    return any(marker in cleaned for marker in own_markers)


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
        log = logger.bind(chat=event.chat_id, author=event.author_username)
        log.info(
            f"ChatHandler.on_message: text={event.text[:80]!r} "
            f"is_my={event.is_my_message}"
        )

        if event.is_my_message:
            log.debug("ChatHandler: пропускаю — is_my_message=True")
            return

        # Подстраховка: сравниваем author_username с моим ником.
        # FunPay не всегда отдаёт data-author в HTML, и фильтр в watcher
        # может ошибиться. Реагировать на свои же сообщения категорически
        # нельзя.
        my_username = getattr(self._fp, "my_username", None)
        if (
            my_username
            and event.author_username
            and event.author_username.strip().lower()
            == my_username.strip().lower()
        ):
            log.info(
                f"ChatHandler: пропускаю — автор @{event.author_username} "
                f"совпадает с моим ником (self-message)"
            )
            return

        text = _clean_chat_text(event.text or "")
        if not text:
            log.debug("ChatHandler: пустой текст после strip — пропуск")
            return

        if _looks_like_own_template_message(text):
            log.info(
                "ChatHandler: пропускаю — текст похож на исходящий шаблон "
                "бота, FunPay отдал его как входящее сообщение"
            )
            return

        async with session_factory()() as session:
            await get_or_create_chat_state(
                session, chat_id=event.chat_id, buyer_username=event.author_username
            )
            await session.commit()

        if _has_help_trigger(text, self._triggers):
            log.info(f"ChatHandler: HELP-триггер найден в тексте → help-flow")
            await self._handle_help_request(event, state_chat_id=event.chat_id)
            return

        if self._settings.chat_autogreeting_enabled:
            log.debug("ChatHandler: проверяю greeting cooldown")
            await self._maybe_greet(event, state_chat_id=event.chat_id)
        else:
            log.debug("ChatHandler: autogreeting выключен, ничего не отвечаю")

    # ---------- Сценарии ----------

    async def _handle_help_request(
        self, event: FunPayMessageEvent, state_chat_id: int
    ) -> None:
        # Cooldown: если совсем недавно уже подняли тревогу в этом чате,
        # не флудим ни покупателю, ни в Telegram. Покупатель часто пишет
        # "!помощь" два-три раза подряд — этого недостаточно, чтобы
        # дублировать алерты.
        cooldown_seconds = int(self._settings.chat_help_cooldown_seconds)
        if cooldown_seconds > 0:
            async with session_factory()() as session:
                pre_state = await get_or_create_chat_state(
                    session,
                    chat_id=state_chat_id,
                    buyer_username=event.author_username,
                )
                if (
                    pre_state.last_help_request_at is not None
                    and (datetime.utcnow() - pre_state.last_help_request_at).total_seconds()
                    < cooldown_seconds
                ):
                    await session.commit()
                    logger.debug(
                        f"Help-ack в чате {event.chat_id} пропущен: "
                        f"cooldown {cooldown_seconds}s ещё не прошёл"
                    )
                    return
                await session.commit()

        working_now = self._wh.is_working_now()
        reply = templates.help_acknowledged(
            event.author_username, working_now=working_now, wh=self._wh
        )
        ack_sent = False
        try:
            result = await self._fp.send_message(event.chat_id, reply)
            # send_message может вернуть dict с ok=False (fallback провалился)
            ack_sent = not (
                isinstance(result, dict) and result.get("ok") is False
            )
            if ack_sent:
                logger.info(
                    f"ChatHandler: help-ack отправлен в чат {event.chat_id} "
                    f"для @{event.author_username}"
                )
            else:
                logger.warning(
                    f"ChatHandler: help-ack НЕ доставлен в чат {event.chat_id} "
                    f"(FunPay вернул ошибку), но Telegram-уведомление пошлём"
                )
        except Exception as exc:
            logger.opt(exception=exc).warning(
                f"Не отправил help-ack в чат {event.chat_id}: {exc}"
            )

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
                f"ChatHandler: приветствие отправлено в чат {event.chat_id} "
                f"для @{event.author_username} (working_now={working_now})"
            )
        except Exception as exc:
            logger.opt(exception=exc).warning(
                f"Не отправил приветствие в чат {event.chat_id}: {exc}"
            )
