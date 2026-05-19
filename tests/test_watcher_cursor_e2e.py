"""
E2E тесты watcher'а с БД-курсором.

Покрытие:
- первый poll: курсоры записаны для всех чатов, dispatch не было;
- второй poll: новое сообщение → dispatch + курсор продвинулся;
- рестарт watcher'а: курсор из БД читается, старые сообщения НЕ replay'ятся.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base
from src.db.repo import get_chat_cursor, upsert_chat_cursor
from src.funpay.watcher import FunPayWatcher


@pytest_asyncio.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.db.session.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _make_watcher_with_admin():
    fp = MagicMock()
    fp.my_username = "lol228822"
    fp.my_user_id = 42
    fp.account = SimpleNamespace(id=42, username="lol228822")
    admin = MagicMock()
    fp._admin = admin
    return fp, admin, FunPayWatcher(fp)


@pytest.mark.asyncio
async def test_first_run_without_unread_does_not_dispatch_or_http(db_factory):
    """
    На baseline watcher НЕ должен делать HTTP-запросы к обычным
    прочитанным чатам без БД-курсора (иначе FunPay даёт 429 на 50 чатах
    подряд). Просто запоминает preview в in-memory snapshot.
    """
    fp, admin, w = _make_watcher_with_admin()
    chat_id = 100

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "старое", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9000, "author_id": 1, "author_username": "buyer1", "text": "когда выдача?"},
        {"message_id": 9001, "author_id": 1, "author_username": "buyer1", "text": "старое"},
    ])

    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new

    await w._poll_once_async()

    assert handled == [], "Первый прогон не должен dispatch'ить старые сообщения"
    admin.get_chat_messages.assert_not_called(), \
        "На baseline НЕ должно быть HTTP-запросов к get_chat_messages"

    assert w._poll_snapshot.get(chat_id, {}).get("preview") == "старое", \
        "preview должен быть запомнен в in-memory snapshot"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor is None, \
            "Курсор в БД на baseline НЕ создаётся (он появится при первом реальном изменении)"


@pytest.mark.asyncio
async def test_first_run_unread_help_is_dispatched(db_factory):
    """
    Критичная регрессия dad6423/a4da7f4:
    если покупатель написал !помощь прямо перед рестартом, первый snapshot
    видел preview='!помощь', сохранял его как baseline и НЕ заходил в чат.
    После этого повторный такой же !помощь не менял preview — бот молчал.

    Теперь unread-чат на baseline ограниченно fetch'ится и последнее
    сообщение уходит в ChatHandler.
    """
    fp, admin, w = _make_watcher_with_admin()
    chat_id = 100

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "!помощь", "unread": True},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9002, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new
    await w._poll_once_async()
    await asyncio.sleep(0.05)

    assert len(handled) == 1
    assert handled[0].text == "!помощь"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor is not None
        assert cursor.last_message_id == 9002


@pytest.mark.asyncio
async def test_second_poll_dispatches_only_new_messages(db_factory):
    fp, admin, w = _make_watcher_with_admin()
    chat_id = 100

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "старое", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9001, "author_id": 1, "author_username": "buyer1", "text": "старое"},
    ])
    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new

    await w._poll_once_async()
    assert handled == []

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "!помощь", "unread": True},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9001, "author_id": 1, "author_username": "buyer1", "text": "старое"},
        {"message_id": 9002, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    await w._poll_once_async()
    # asyncio.create_task внутри poll → даём task'у пробежать
    await asyncio.sleep(0.05)
    # На первом реальном изменении preview мы видим last message
    # (cursor_last_id == None → диспатчим только последнее)
    assert len(handled) == 1
    assert handled[0].text == "!помощь"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor.last_message_id == 9002


@pytest.mark.asyncio
async def test_restart_does_not_replay_old_messages(db_factory):
    """Симулируем рестарт: новый watcher с уже заполненной БД-курсорами."""
    fp, admin, w1 = _make_watcher_with_admin()
    chat_id = 100

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "!помощь", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9001, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    handled1: list = []

    async def _on_new1(msg):
        handled1.append(msg)

    w1._on_new_message = _on_new1

    await w1._poll_once_async()
    assert handled1 == []

    # Симулируем что у w1 уже был "первый цикл" + одно реальное изменение,
    # курсор записан в БД.
    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "x", "unread": True},
    ])
    await w1._poll_once_async()
    await asyncio.sleep(0.05)

    # Новый watcher (рестарт)
    _, _, w2 = _make_watcher_with_admin()
    w2._fp._admin = admin
    handled2: list = []

    async def _on_new2(msg):
        handled2.append(msg)

    w2._on_new_message = _on_new2

    # На baseline — HTTP не вызывается
    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "x", "unread": False},
    ])
    await w2._poll_once_async()
    await asyncio.sleep(0.05)
    assert handled2 == []

    # Теперь второй poll: preview НЕ изменился → нет HTTP → нет dispatch
    await w2._poll_once_async()
    await asyncio.sleep(0.05)
    assert handled2 == [], "При неизменённом preview не должно быть dispatch"


@pytest.mark.asyncio
async def test_first_run_with_existing_cursor_catches_messages_missed_while_down(db_factory):
    """
    Если сервис был выключен во время update.sh, БД-курсор уже есть.
    Первый poll после старта должен ограниченно проверить такие чаты и
    догнать сообщения с id > cursor, даже если unread-флаг в HTML не сработал.
    """
    fp, admin, w = _make_watcher_with_admin()
    chat_id = 100

    async with db_factory() as session:
        await upsert_chat_cursor(session, chat_id=chat_id, last_message_id=9001)
        await session.commit()

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "!помощь", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9002, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new
    await w._poll_once_async()
    await asyncio.sleep(0.05)

    assert len(handled) == 1
    assert handled[0].text == "!помощь"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor.last_message_id == 9002


@pytest.mark.asyncio
async def test_repeated_same_preview_help_is_caught_by_active_poll(db_factory):
    """
    Реальный кейс с продакшена:
    покупатель пишет !помощь, бот отвечает; через несколько минут покупатель
    снова пишет точно такой же !помощь. В левой панели preview может остаться
    тем же самым, поэтому diff по preview не срабатывает. Active poll верхних
    чатов должен открыть историю и увидеть новый message_id.
    """
    fp, admin, w = _make_watcher_with_admin()
    chat_id = 100

    async with db_factory() as session:
        await upsert_chat_cursor(session, chat_id=chat_id, last_message_id=9002)
        await session.commit()

    w._baseline_ready.set()
    w._poll_snapshot[chat_id] = {"preview": "!помощь"}

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "!помощь", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9003, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new
    await w._poll_once_async()
    await asyncio.sleep(0.05)

    assert len(handled) == 1
    assert handled[0].text == "!помощь"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor.last_message_id == 9003


@pytest.mark.asyncio
async def test_same_help_text_with_different_message_ids_is_not_deduped(db_factory):
    """
    Дедуп должен работать по message_id, а не по тексту, когда id есть.
    Иначе серия одинаковых !помощь превращается в "дубликаты" и молча
    пропадает после первого ответа.
    """
    fp, admin, w = _make_watcher_with_admin()
    chat_id = 100

    async with db_factory() as session:
        await upsert_chat_cursor(session, chat_id=chat_id, last_message_id=9002)
        await session.commit()

    w._baseline_ready.set()
    w._poll_snapshot[chat_id] = {"preview": "!помощь"}
    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "!помощь", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9003, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    await w._poll_once_async()
    await asyncio.sleep(0.05)

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "!помощь", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9004, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    await w._poll_once_async()
    await asyncio.sleep(0.05)

    assert [m.text for m in handled] == ["!помощь", "!помощь"]

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor.last_message_id == 9004


@pytest.mark.asyncio
async def test_new_chat_appears_after_first_run_processes_last_message(db_factory):
    fp, admin, w = _make_watcher_with_admin()

    admin.get_chats_snapshot = AsyncMock(return_value=[])
    admin.get_chat_messages = AsyncMock(return_value=[])
    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new

    # baseline c пустым snapshot
    await w._poll_once_async()

    chat_id = 555
    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "K1kern", "preview": "Привет", "unread": True},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 7777, "author_id": 1, "author_username": "K1kern", "text": "Привет"},
    ])
    # Этот poll — НЕ baseline (он уже done в w). Чат новый → cursor=None →
    # диспатчим только последнее сообщение из истории.
    await w._poll_once_async()
    await asyncio.sleep(0.05)

    assert len(handled) == 1
    assert handled[0].text == "Привет"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor.last_message_id == 7777
