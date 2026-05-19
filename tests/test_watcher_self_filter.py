"""
Watcher НЕ должен реагировать на собственные сообщения.

Сценарий, который мы ловим:
- Бот ответил приветствием с фразой `!помощь`.
- Watcher через `get_chat_messages` забирает все сообщения чата
  (включая это собственное).
- Если фильтр по author_id не сработал (HTML без `data-author`),
  фильтр по author_username (= my_username) обязан спасти.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.funpay.events import FunPayMessageEvent
from src.funpay.watcher import FunPayWatcher


def _make_watcher(my_username: str = "lol228822", my_user_id: int | None = None):
    fp = MagicMock()
    fp.my_username = my_username
    fp.my_user_id = my_user_id
    fp.account = SimpleNamespace(id=my_user_id, username=my_username)
    return FunPayWatcher(fp)


def test_is_my_message_by_id():
    w = _make_watcher(my_user_id=42)
    assert w._is_my_message(author_id=42, author_username="someone") is True
    assert w._is_my_message(author_id=43, author_username="someone") is False


def test_is_my_message_by_username_case_insensitive():
    w = _make_watcher(my_username="lol228822", my_user_id=None)
    # Без id — спасает только сравнение username
    assert w._is_my_message(author_id=None, author_username="lol228822") is True
    assert w._is_my_message(author_id=None, author_username="LOL228822") is True
    assert w._is_my_message(author_id=None, author_username=" lol228822 ") is True
    assert w._is_my_message(author_id=None, author_username="VOLT228822") is False


def test_is_my_message_handles_none_username_safely():
    w = _make_watcher(my_username="lol228822", my_user_id=42)
    assert w._is_my_message(author_id=None, author_username=None) is False


def test_dedup_listen_without_id_does_not_block_poll_with_id():
    """
    Если poll даёт message_id, id считается главным источником правды.
    Text-hash от listen-loop без id НЕ должен блокировать poll-сообщение,
    иначе повторные одинаковые !помощь пропадают.
    """
    w = _make_watcher()
    chat_id = 104433092

    listen_event = FunPayMessageEvent(
        chat_id=chat_id,
        chat_username="VOLT228822",
        author_id=2,
        author_username="VOLT228822",
        text="!помощь",
        is_my_message=False,
    )
    assert w._dedup_register("msg", listen_event) is True

    poll_keys = w._make_msg_dedup_keys(chat_id, 5_555_555, 2, "!помощь")
    assert w._seen_or_register(poll_keys) is False


def test_dedup_poll_with_id_does_not_register_text_fallback():
    """Poll-сообщение с id не должно занимать text-key."""
    w = _make_watcher()
    chat_id = 104433092

    poll_keys = w._make_msg_dedup_keys(chat_id, 5_555_555, 2, "!помощь")
    assert w._seen_or_register(poll_keys) is False

    listen_event = FunPayMessageEvent(
        chat_id=chat_id,
        chat_username="VOLT228822",
        author_id=2,
        author_username="VOLT228822",
        text="!помощь",
        is_my_message=False,
    )
    assert w._dedup_register("msg", listen_event) is True


def test_dedup_different_messages_not_collapsed():
    w = _make_watcher()
    k1 = w._make_msg_dedup_keys(123, None, 1, "тест")
    k2 = w._make_msg_dedup_keys(123, None, 1, "другое")
    assert w._seen_or_register(k1) is False
    assert w._seen_or_register(k2) is False


def test_dedup_same_text_different_message_ids_are_not_duplicates():
    """
    Одинаковый текст с разными message_id — это разные сообщения.
    Например покупатель три раза подряд пишет !помощь.
    """
    w = _make_watcher()
    k1 = w._make_msg_dedup_keys(123, 111, 1, "одинаковый текст")
    assert w._seen_or_register(k1) is False
    k2 = w._make_msg_dedup_keys(123, 222, 1, "одинаковый текст")
    assert w._seen_or_register(k2) is False
