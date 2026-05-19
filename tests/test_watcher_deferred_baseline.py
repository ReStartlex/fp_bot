"""
Тесты на «выбор новых сообщений» в watcher с учётом БД-курсора.

Сценарии:
- cursor известен → диспатчим только id > cursor (точная защита от replay);
- cursor None + первый прогон → ничего не диспатчим, только сохраняем max(id);
- cursor None + runtime (новый покупатель) → диспатчим последнее сообщение.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.funpay.watcher import FunPayWatcher


def _make_watcher():
    fp = MagicMock()
    fp.my_username = "lol228822"
    fp.my_user_id = 42
    fp.account = SimpleNamespace(id=42, username="lol228822")
    return FunPayWatcher(fp)


def test_select_with_cursor_filters_strictly_greater():
    w = _make_watcher()
    messages = [
        {"message_id": 100, "text": "старое"},
        {"message_id": 101, "text": "тоже старое"},
        {"message_id": 102, "text": "новое 1"},
        {"message_id": 103, "text": "новое 2"},
    ]
    new, last_id = w._select_new_messages(
        messages=messages, cursor_last_id=101, is_first_run=False
    )
    assert [m["text"] for m in new] == ["новое 1", "новое 2"]
    assert last_id == 103


def test_select_first_run_with_no_cursor_only_records_max_id():
    """Самый первый прогон watcher'а: историю не разыгрываем."""
    w = _make_watcher()
    messages = [
        {"message_id": 100, "text": "до"},
        {"message_id": 101, "text": "!помощь"},
        {"message_id": 102, "text": "ещё"},
    ]
    new, last_id = w._select_new_messages(
        messages=messages, cursor_last_id=None, is_first_run=True
    )
    assert new == []
    assert last_id == 102


def test_select_runtime_with_no_cursor_picks_last_message():
    """Новый покупатель после baseline — обрабатываем последнее в выборке."""
    w = _make_watcher()
    messages = [
        {"message_id": 200, "text": "ой"},
        {"message_id": 201, "text": "Привет"},
    ]
    new, last_id = w._select_new_messages(
        messages=messages, cursor_last_id=None, is_first_run=False
    )
    assert [m["text"] for m in new] == ["Привет"]
    assert last_id == 201


def test_select_runtime_with_no_cursor_handles_empty_messages():
    w = _make_watcher()
    new, last_id = w._select_new_messages(
        messages=[], cursor_last_id=None, is_first_run=False
    )
    assert new == []
    assert last_id is None


def test_select_with_cursor_skips_when_no_new():
    w = _make_watcher()
    messages = [
        {"message_id": 100, "text": "уже видели"},
        {"message_id": 101, "text": "и это уже"},
    ]
    new, last_id = w._select_new_messages(
        messages=messages, cursor_last_id=101, is_first_run=False
    )
    assert new == []
    # last_id двигаем до max(id), даже если новых нет — снижает шум
    assert last_id == 101
