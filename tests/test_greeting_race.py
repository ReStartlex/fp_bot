"""
Дубль `greeting_pre_purchase` при двойном дispatch одного сообщения watcher-ом.

Сценарий из прода (источник дубля на скрине в чате):
1) Покупатель пишет в чат «Не получается оплатить» — одно входящее.
2) watcher.listen-loop ловит событие БЕЗ message_id (FunPayAPI 1.1.0
   не отдаёт id в listen) → dedup ключ = ("msg", chat, "text", author, hash).
3) Через 1–5 сек watcher.poll-loop ловит ТО ЖЕ сообщение, но УЖЕ С
   message_id → dedup ключ = ("msg", chat, "id", 12345). Эти ключи разные
   → дедуп их НЕ ловит → handler.on_message вызывается ДВАЖДЫ
   параллельно (asyncio.create_task / run_coroutine_threadsafe).
4) Оба таска идут в `_maybe_greet`. Старый код:
   - оба читают `state.greeted_at` ДО commit'а другого таска — оба видят None;
   - оба делают `mark_greeted` + commit (idempotent на уровне БД);
   - оба зовут `fp.send_message(...)` → в чате покупателя ДВА
     одинаковых приветствия.

Защита: `mark_greeted_if_due` использует атомарный conditional UPDATE
(`WHERE greeted_at IS NULL OR greeted_at < cutoff`) и возвращает True
только тому таску, который реально обновил строку. Проигравший таск
не отправляет сообщение в FunPay.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.chat.handler import ChatHandler
from src.config import Settings
from src.db.models import Base
from src.funpay.events import FunPayMessageEvent


@pytest_asyncio.fixture()
async def session_factory_fixture(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.chat.handler.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _settings(*, autogreeting: bool = True, cooldown_hours: int = 24) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1,
        ns_login="x",
        ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x",
        funpay_user_id=1,
        chat_autogreeting_enabled=autogreeting,
        chat_greeting_cooldown_hours=cooldown_hours,
    )


def _make_handler(settings: Settings) -> tuple[ChatHandler, MagicMock]:
    fp = MagicMock()
    fp.my_username = "lol228822"
    fp.account = SimpleNamespace(id=1, username="lol228822")
    fp.send_message = AsyncMock()
    tg = MagicMock()
    tg.send = AsyncMock()
    return ChatHandler(fp, telegram=tg, settings=settings), fp


def _buyer_event(text: str, chat_id: int = 9001) -> FunPayMessageEvent:
    return FunPayMessageEvent(
        chat_id=chat_id,
        chat_username="Americancow",
        author_id=4242,
        author_username="Americancow",
        text=text,
        is_my_message=False,
    )


# ─────────────── Главный тест на race ───────────────


@pytest.mark.asyncio
async def test_two_concurrent_messages_send_only_one_greeting(
    session_factory_fixture,
):
    """
    Watcher продublirовал одно сообщение через listen и poll (разные
    ключи дедупа). На handler-уровне обе задачи выполняются параллельно.
    Бот должен ответить ровно одним приветствием.
    """
    handler, fp = _make_handler(_settings())

    async def slow_send(*_args, **_kwargs) -> None:
        # Имитируем сетевую задержку до FunPay, чтобы окно гонки
        # «check → act» гарантированно открылось для второго таска.
        await asyncio.sleep(0.05)

    fp.send_message = AsyncMock(side_effect=slow_send)

    event = _buyer_event("Не получается оплатить")
    await asyncio.gather(
        handler.on_message(event),
        handler.on_message(event),
    )

    assert fp.send_message.call_count == 1, (
        "RACE: при параллельном dispatch одного сообщения watcher-ом "
        f"бот отправил приветствие {fp.send_message.call_count} раз "
        "(ожидается 1). Нужен атомарный conditional UPDATE для greeted_at."
    )


# ─────────────── Тесты на нормальное поведение cooldown ───────────────


@pytest.mark.asyncio
async def test_sequential_messages_within_cooldown_send_one_greeting(
    session_factory_fixture,
):
    """
    Базовое поведение cooldown: второе сообщение от того же покупателя
    в течение 24ч не должно вызывать второе приветствие.
    """
    handler, fp = _make_handler(_settings(cooldown_hours=24))

    await handler.on_message(_buyer_event("Здравствуйте"))
    await handler.on_message(_buyer_event("Когда выдадите?"))

    assert fp.send_message.call_count == 1


@pytest.mark.asyncio
async def test_different_chats_get_independent_greetings(
    session_factory_fixture,
):
    """Анти-регрессия: cooldown — per-chat, не глобальный."""
    handler, fp = _make_handler(_settings())

    await handler.on_message(_buyer_event("Здравствуйте", chat_id=1))
    await handler.on_message(_buyer_event("Здравствуйте", chat_id=2))

    assert fp.send_message.call_count == 2


@pytest.mark.asyncio
async def test_autogreeting_disabled_sends_nothing(
    session_factory_fixture,
):
    """Анти-регрессия: при выключенном autogreeting приветствий нет."""
    handler, fp = _make_handler(_settings(autogreeting=False))

    await handler.on_message(_buyer_event("Здравствуйте"))
    await handler.on_message(_buyer_event("Здравствуйте"))

    assert fp.send_message.call_count == 0
