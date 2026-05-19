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


def test_dedup_key_stable_across_listen_and_poll():
    """Ключ дедупа должен быть одинаковым для listen и poll, чтобы
    сообщение, увиденное обоими источниками, обрабатывалось один раз."""
    w = _make_watcher()

    chat_id = 104433092
    msg_id = 5_555_555

    poll_key = w._make_msg_dedup_key(chat_id, msg_id, 1, "тест")
    listen_event = FunPayMessageEvent(
        chat_id=chat_id,
        chat_username="VOLT228822",
        author_id=1,
        author_username="VOLT228822",
        text="тест",
        is_my_message=False,
    )
    setattr(listen_event, "message_id", msg_id)

    assert w._dedup_register("msg", listen_event) is True
    # listen уже зарегистрировал ключ — poll-фронт должен видеть его как дубль
    with w._seen_lock:
        assert poll_key in w._seen_keys


def test_dedup_key_fallback_when_no_message_id_matches_text_hash():
    w = _make_watcher()
    k1 = w._make_msg_dedup_key(123, None, 1, "тест")
    k2 = w._make_msg_dedup_key(123, None, 1, "тест")
    assert k1 == k2
    # разный текст -> разный ключ
    assert w._make_msg_dedup_key(123, None, 1, "другое") != k1


def test_dedup_key_with_message_id_ignores_text():
    """Если есть message_id, на текст не смотрим — это самое надёжное."""
    w = _make_watcher()
    k1 = w._make_msg_dedup_key(123, 999, 1, "тест")
    k2 = w._make_msg_dedup_key(123, 999, 1, "совсем другой текст")
    assert k1 == k2
