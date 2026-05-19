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
from src.db.repo import get_chat_cursor
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
async def test_first_run_writes_cursors_without_dispatch(db_factory):
    fp, admin, w = _make_watcher_with_admin()
    chat_id = 100

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "старое", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9000, "author_id": 1, "author_username": "buyer1", "text": "когда выдача?"},
        {"message_id": 9001, "author_id": 1, "author_username": "buyer1", "text": "старое"},
    ])

    dispatched: list = []
    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new

    await w._poll_once_async()

    assert handled == [], "Первый прогон не должен dispatch'ить старые сообщения"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor is not None
        assert cursor.last_message_id == 9001


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
        {"message_id": 9002, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    await w._poll_once_async()
    # asyncio.create_task внутри poll → даём task'у пробежать
    await asyncio.sleep(0.05)
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

    _, _, w2 = _make_watcher_with_admin()
    w2._fp._admin = admin
    handled2: list = []

    async def _on_new2(msg):
        handled2.append(msg)

    w2._on_new_message = _on_new2
    await w2._poll_once_async()
    await asyncio.sleep(0.05)
    assert handled2 == []


@pytest.mark.asyncio
async def test_new_chat_appears_after_first_run_processes_last_message(db_factory):
    fp, admin, w = _make_watcher_with_admin()

    admin.get_chats_snapshot = AsyncMock(return_value=[])
    admin.get_chat_messages = AsyncMock(return_value=[])
    handled: list = []

    async def _on_new(msg):
        handled.append(msg)

    w._on_new_message = _on_new

    await w._poll_once_async()

    chat_id = 555
    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "K1kern", "preview": "Привет", "unread": True},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 7777, "author_id": 1, "author_username": "K1kern", "text": "Привет"},
    ])
    await w._poll_once_async()
    await asyncio.sleep(0.05)

    assert len(handled) == 1
    assert handled[0].text == "Привет"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor.last_message_id == 7777
