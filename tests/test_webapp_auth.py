"""
Sprint 6 — тесты валидации Telegram WebApp initData.

Критически важно: эта функция — единственный gate для Mini App auth.
Любая дыра здесь = чужой юзер может подделать запросы.

Покрываем:
  * Валидный initData с правильным hash — успех
  * Битый hash → 401
  * Истёкший auth_date → 401
  * auth_date в будущем → 401
  * Missing hash / user / auth_date → 401
  * Подменённый user (hash из старого initData) → 401
  * Wrong bot_token → 401
  * Empty / non-URL string → 401
  * Битый JSON в user → 401
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from src.api.webapp_auth import (
    WebAppAuthError,
    _compute_secret_key,
    verify_init_data,
)


BOT_TOKEN = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def make_init_data(
    *,
    bot_token: str = BOT_TOKEN,
    user_id: int = 12345,
    auth_date: int | None = None,
    extras: dict | None = None,
    use_wrong_hash: bool = False,
    tamper_user_after_sign: bool = False,
) -> str:
    """
    Конструирует валидный (или предсказуемо-битый) init_data для тестов.

    Возвращает URL-encoded string как Telegram пришлёт фронту.
    """
    if auth_date is None:
        auth_date = int(time.time())
    user_obj = {
        "id": user_id,
        "first_name": "Alice",
        "username": "alice",
        "language_code": "ru",
    }
    fields = {
        "user": json.dumps(user_obj, separators=(",", ":")),
        "auth_date": str(auth_date),
        "query_id": "AAEAAA",
        "chat_instance": "-9876543210",
        "chat_type": "private",
    }
    if extras:
        fields.update(extras)

    # data_check_string алфавитно отсортированный
    data_check_string = "\n".join(
        f"{k}={fields[k]}" for k in sorted(fields.keys())
    )
    secret = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if use_wrong_hash:
        expected_hash = "deadbeef" * 8

    # Подмена user после подписи: атака «изменяю userId, оставляю старый hash»
    if tamper_user_after_sign:
        user_obj["id"] = 99999
        fields["user"] = json.dumps(user_obj, separators=(",", ":"))

    return urlencode({**fields, "hash": expected_hash})


# ─── Happy path ────────────────────────────────────────────────────


def test_verify_happy_path():
    init_data = make_init_data(user_id=42)
    result = verify_init_data(init_data, bot_token=BOT_TOKEN)
    assert result.user.id == 42
    assert result.user.first_name == "Alice"
    assert result.user.username == "alice"
    assert result.user.language_code == "ru"
    assert result.query_id == "AAEAAA"


def test_verify_returns_premium_flag():
    """is_premium из user должен подняться в WebAppUser."""
    init_data = make_init_data(extras={
        "user": json.dumps({
            "id": 7,
            "first_name": "Bob",
            "is_premium": True,
        }, separators=(",", ":")),
    })
    # Хеш для этого init_data сейчас невалидный — пересоберём правильно
    fresh = make_init_data(user_id=7)
    # Подменим только данные user через build helper
    custom_user = {
        "id": 7, "first_name": "Bob", "is_premium": True,
    }
    auth_date = int(time.time())
    fields = {
        "user": json.dumps(custom_user, separators=(",", ":")),
        "auth_date": str(auth_date),
    }
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields.keys()))
    secret = _compute_secret_key(BOT_TOKEN)
    h = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    init_data = urlencode({**fields, "hash": h})
    result = verify_init_data(init_data, bot_token=BOT_TOKEN)
    assert result.user.is_premium is True


# ─── Tampering / wrong hash ───────────────────────────────────────


def test_verify_rejects_wrong_hash():
    init_data = make_init_data(use_wrong_hash=True)
    with pytest.raises(WebAppAuthError, match="hash mismatch"):
        verify_init_data(init_data, bot_token=BOT_TOKEN)


def test_verify_rejects_wrong_bot_token():
    """Тот же initData → другой bot_token → отказ."""
    init_data = make_init_data()
    with pytest.raises(WebAppAuthError, match="hash mismatch"):
        verify_init_data(init_data, bot_token="9999999:ZZZ")


def test_verify_rejects_tampered_user_after_signing():
    """
    Реальная атака: атакующий перехватил initData юзера 12345,
    поменял user.id на 99999, попытался переиспользовать.
    """
    init_data = make_init_data(
        user_id=12345, tamper_user_after_sign=True,
    )
    with pytest.raises(WebAppAuthError, match="hash mismatch"):
        verify_init_data(init_data, bot_token=BOT_TOKEN)


# ─── auth_date checks ─────────────────────────────────────────────


def test_verify_rejects_expired_init_data():
    """initData старше max_age_seconds → отказ."""
    init_data = make_init_data(
        auth_date=int(time.time()) - 25 * 3600,  # 25 часов назад
    )
    with pytest.raises(WebAppAuthError, match="expired"):
        verify_init_data(
            init_data, bot_token=BOT_TOKEN, max_age_seconds=24 * 3600,
        )


def test_verify_accepts_fresh_init_data():
    """initData выпущен 1 час назад — норма."""
    init_data = make_init_data(
        auth_date=int(time.time()) - 3600,
    )
    result = verify_init_data(init_data, bot_token=BOT_TOKEN)
    assert result.user.id == 12345


def test_verify_rejects_future_auth_date():
    """auth_date в далёком будущем (> 5 min skew) → отказ."""
    init_data = make_init_data(
        auth_date=int(time.time()) + 3600,
    )
    with pytest.raises(WebAppAuthError, match="future"):
        verify_init_data(init_data, bot_token=BOT_TOKEN)


def test_verify_tolerates_small_clock_skew():
    """Расхождение часов до 5 минут — терпим."""
    init_data = make_init_data(
        auth_date=int(time.time()) + 60,  # 1 минута в будущем
    )
    # Не должно бросать
    result = verify_init_data(init_data, bot_token=BOT_TOKEN)
    assert result.user.id == 12345


# ─── Malformed input ──────────────────────────────────────────────


def test_verify_rejects_empty_string():
    with pytest.raises(WebAppAuthError, match="empty"):
        verify_init_data("", bot_token=BOT_TOKEN)


def test_verify_rejects_missing_hash():
    """Поля есть, но hash отсутствует."""
    fields = {
        "user": '{"id":1}',
        "auth_date": str(int(time.time())),
    }
    init_data = urlencode(fields)
    with pytest.raises(WebAppAuthError, match="hash missing"):
        verify_init_data(init_data, bot_token=BOT_TOKEN)


def test_verify_rejects_missing_user():
    """hash правильный, но user-поля нет → отказ (auth_date один не достаточен)."""
    auth_date = int(time.time())
    fields = {"auth_date": str(auth_date)}
    dcs = f"auth_date={auth_date}"
    secret = _compute_secret_key(BOT_TOKEN)
    h = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    init_data = urlencode({**fields, "hash": h})
    with pytest.raises(WebAppAuthError, match="user missing"):
        verify_init_data(init_data, bot_token=BOT_TOKEN)


def test_verify_rejects_missing_auth_date():
    """user есть, auth_date — нет."""
    fields = {"user": '{"id":1}'}
    dcs = 'user={"id":1}'
    secret = _compute_secret_key(BOT_TOKEN)
    h = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    init_data = urlencode({**fields, "hash": h})
    with pytest.raises(WebAppAuthError, match="auth_date missing"):
        verify_init_data(init_data, bot_token=BOT_TOKEN)


def test_verify_rejects_malformed_user_json():
    """user='not valid json' → отказ."""
    auth_date = int(time.time())
    fields = {
        "user": "{not_valid_json",
        "auth_date": str(auth_date),
    }
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields.keys()))
    secret = _compute_secret_key(BOT_TOKEN)
    h = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    init_data = urlencode({**fields, "hash": h})
    with pytest.raises(WebAppAuthError, match="valid JSON"):
        verify_init_data(init_data, bot_token=BOT_TOKEN)


def test_verify_rejects_user_without_id():
    """user без id — отказ."""
    auth_date = int(time.time())
    user_json = json.dumps({"first_name": "X"}, separators=(",", ":"))
    fields = {
        "user": user_json,
        "auth_date": str(auth_date),
    }
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields.keys()))
    secret = _compute_secret_key(BOT_TOKEN)
    h = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    init_data = urlencode({**fields, "hash": h})
    with pytest.raises(WebAppAuthError, match="user.id missing"):
        verify_init_data(init_data, bot_token=BOT_TOKEN)


def test_verify_rejects_empty_bot_token():
    init_data = make_init_data()
    with pytest.raises(WebAppAuthError, match="bot_token not configured"):
        verify_init_data(init_data, bot_token="")
