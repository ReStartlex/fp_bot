"""
Тест на «отложенный baseline»: если baseline не записал last_message_id
для какого-то чата, watcher НЕ должен реагировать на старую историю,
а просто запомнить максимальный id и продолжить.

Это критический баг: при сбое парсинга baseline бот срывал триггеры на
старые сообщения (например, реагировал на старое "!помощь" как на новое).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.funpay.watcher import FunPayWatcher


def _make_watcher():
    fp = MagicMock()
    fp.my_username = "lol228822"
    fp.my_user_id = 42
    fp.account = SimpleNamespace(id=42, username="lol228822")

    admin = MagicMock()
    fp._admin = admin
    return fp, admin, FunPayWatcher(fp)


def _run(coro):
    """Прогоняем asyncio.run-like синхронно для тестов."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def test_deferred_baseline_does_not_replay_old_messages(monkeypatch):
    """
    Сценарий:
    1. На baseline _fetch_last_message_id вернул None (HTML парсинг
       не нашёл id у сообщений) → last_id остался None.
    2. Через 5 сек покупатель пишет "привет".
    3. Watcher тянет историю, видит 5 старых сообщений (включая "!помощь").
    4. БАГ был: все 5 сообщений считались новыми, бот реагировал.
    5. ФИКС: watcher должен пропустить весь batch (отложенный baseline)
       и записать max(id). Никаких dispatch.
    """
    fp, admin, w = _make_watcher()

    chat_id = 999

    # Сначала baseline: snapshot есть, но last_id=None
    w._poll_snapshot[chat_id] = {"preview": "старое сообщение", "last_id": None}
    w._baseline_ready.set()

    # admin.get_chats_snapshot() возвращает обновлённый preview
    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "VOLT228822", "preview": "привет", "unread": False},
    ])
    # admin.get_chat_messages() возвращает старую историю с !помощь
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 1001, "author_id": 2, "author_username": "VOLT228822", "text": "!помощь"},
        {"message_id": 1002, "author_id": 42, "author_username": "lol228822", "text": "Принято"},
        {"message_id": 1003, "author_id": 2, "author_username": "VOLT228822", "text": "привет"},
    ])

    dispatched: list = []
    w._on_new_message = lambda m: dispatched.append(m) or _NoopAwaitable()  # type: ignore
    w._dispatch_async = lambda coro: dispatched.append("dispatched")  # type: ignore
    w._loop = None  # отключаем реальный async dispatch

    w._poll_once()

    # ни одного dispatch — все сообщения считаются «до baseline»
    assert dispatched == [], f"Не должно было ничего диспатчить, а пришло: {dispatched}"
    # last_id обновился до максимума из batch
    assert w._poll_snapshot[chat_id]["last_id"] == 1003


def test_subsequent_poll_after_deferred_baseline_processes_only_new(monkeypatch):
    """
    После «отложенного baseline» (где мы записали last_id=1003) следующий
    poll должен видеть как «новое» только сообщения с id > 1003.
    """
    fp, admin, w = _make_watcher()
    chat_id = 999

    # baseline уже завершён, last_id=1003 (после первого poll-once)
    w._poll_snapshot[chat_id] = {"preview": "привет", "last_id": 1003}
    w._baseline_ready.set()

    admin.get_chats_snapshot = AsyncMock(return_value=[
        {"chat_id": chat_id, "username": "VOLT228822", "preview": "как дела?", "unread": False},
    ])
    admin.get_chat_messages = AsyncMock(return_value=[
        {"message_id": 1004, "author_id": 2, "author_username": "VOLT228822", "text": "как дела?"},
    ])

    dispatched: list = []
    w._dispatch_async = lambda coro: dispatched.append(coro)  # type: ignore
    w._on_new_message = AsyncMock()
    w._loop = None  # отключаем реальный async dispatch

    w._poll_once()

    # один dispatch на единственное новое сообщение
    assert len(dispatched) == 1
    assert w._poll_snapshot[chat_id]["last_id"] == 1004


class _NoopAwaitable:
    def __await__(self):
        if False:
            yield
        return None
