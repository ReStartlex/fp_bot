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
    w._dispatch_async = lambda coro: dispatched.append(coro)  # type: ignore
    w._on_new_message = AsyncMock()
    w._loop = None

    # Запускаем _poll_once в потоке executor (он внутри использует asyncio.run)
    await asyncio.to_thread(w._poll_once)

    assert dispatched == [], "Первый прогон не должен dispatch'ить старые сообщения"

    # Курсор записан в БД
    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor is not None
        assert cursor.last_message_id == 9001


@pytest.mark.asyncio
async def test_second_poll_dispatches_only_new_messages(db_factory):
    fp, admin, w = _make_watcher_with_admin()
    chat_id = 100

    # ── 1) первый poll: baseline ──
    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "старое", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9001, "author_id": 1, "author_username": "buyer1", "text": "старое"},
    ])
    dispatched: list = []
    w._dispatch_async = lambda coro: dispatched.append(coro)  # type: ignore
    w._on_new_message = AsyncMock()
    w._loop = None
    await asyncio.to_thread(w._poll_once)
    assert dispatched == []

    # ── 2) пришло новое сообщение ──
    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "buyer1", "preview": "!помощь", "unread": True},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 9002, "author_id": 1, "author_username": "buyer1", "text": "!помощь"},
    ])
    await asyncio.to_thread(w._poll_once)
    assert len(dispatched) == 1

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
    dispatched1: list = []
    w1._dispatch_async = lambda coro: dispatched1.append(coro)  # type: ignore
    w1._on_new_message = AsyncMock()
    w1._loop = None
    await asyncio.to_thread(w1._poll_once)
    # На первом прогоне ничего не диспатчилось — это правильно
    assert dispatched1 == []

    # Имитируем рестарт: новый watcher с тем же fp
    _, _, w2 = _make_watcher_with_admin()
    w2._fp._admin = admin
    dispatched2: list = []
    w2._dispatch_async = lambda coro: dispatched2.append(coro)  # type: ignore
    w2._on_new_message = AsyncMock()
    w2._loop = None
    await asyncio.to_thread(w2._poll_once)
    # курсор из БД (=9001) → "!помощь" с id=9001 НЕ старше → ничего нового
    assert dispatched2 == []


@pytest.mark.asyncio
async def test_new_chat_appears_after_first_run_processes_last_message(db_factory):
    """
    После первого baseline в чате X (X не было в snapshot первого прогона)
    появилось сообщение «Привет» от нового покупателя — должно быть
    обработано.
    """
    fp, admin, w = _make_watcher_with_admin()

    # 1) baseline: пустой snapshot
    admin.get_chats_snapshot = AsyncMock(return_value=[])
    admin.get_chat_messages = AsyncMock(return_value=[])
    dispatched: list = []
    w._dispatch_async = lambda coro: dispatched.append(coro)  # type: ignore
    w._on_new_message = AsyncMock()
    w._loop = None
    await asyncio.to_thread(w._poll_once)

    # 2) появился новый чат
    chat_id = 555
    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "K1kern", "preview": "Привет", "unread": True},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 7777, "author_id": 1, "author_username": "K1kern", "text": "Привет"},
    ])
    await asyncio.to_thread(w._poll_once)

    assert len(dispatched) == 1, "новое сообщение в новом чате должно сработать"

    async with db_factory() as session:
        cursor = await get_chat_cursor(session, chat_id)
        assert cursor.last_message_id == 7777
