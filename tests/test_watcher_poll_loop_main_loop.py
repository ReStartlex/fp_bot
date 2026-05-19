"""
Smoke-тест: убеждаемся что watcher запускает poll-thread, который
выполняет _poll_once_async в main asyncio loop (не через asyncio.run).

Это критично для aiosqlite: её engine привязан к loop'у создания.
Каждый отдельный asyncio.run() в потоке создаёт новый loop и ломает
последующие БД-операции.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.funpay.watcher import FunPayWatcher


@pytest.mark.asyncio
async def test_poll_loop_uses_run_coroutine_threadsafe_on_main_loop():
    """
    Проверяем, что poll-thread:
    1. Видит self._loop = текущий asyncio loop теста.
    2. Вызывает run_coroutine_threadsafe в этот loop (а не asyncio.run).
    """
    fp = MagicMock()
    fp.my_username = "lol228822"
    fp.my_user_id = 999
    fp.account = MagicMock(username="lol228822", id=999)
    fp._admin = MagicMock()
    fp._admin.get_chats_snapshot = AsyncMock(return_value=[])

    w = FunPayWatcher(fp, on_new_message=AsyncMock(), poll_interval_seconds=0.5)
    w._listen_loop = lambda: None  # noqa: E731

    w.start()
    try:
        # Ждём, пока poll-thread проснётся (sleep 2s + 1 итерация)
        await asyncio.sleep(2.5)
        assert w._loop is not None
        assert w._loop is asyncio.get_running_loop()
        assert w._baseline_ready.is_set(), (
            "baseline должен сработать за первый poll-цикл"
        )
        fp._admin.get_chats_snapshot.assert_awaited()
    finally:
        w.stop()


@pytest.mark.asyncio
async def test_poll_loop_recovers_when_admin_raises():
    """Если admin.get_chats_snapshot бросит — poll-loop продолжит крутиться."""
    fp = MagicMock()
    fp.my_username = "lol228822"
    fp.my_user_id = 999
    fp.account = MagicMock(username="lol228822", id=999)
    fp._admin = MagicMock()
    call_count = {"n": 0}

    async def _raising_then_ok():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first call boom")
        return []

    fp._admin.get_chats_snapshot = _raising_then_ok

    w = FunPayWatcher(fp, on_new_message=AsyncMock(), poll_interval_seconds=0.3)
    w._listen_loop = lambda: None  # noqa: E731

    w.start()
    try:
        await asyncio.sleep(3.0)
        assert call_count["n"] >= 2, (
            "после первого исключения poll должен был сделать ещё попытку"
        )
    finally:
        w.stop()
