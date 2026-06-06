"""Тесты верификации Telegram Login Widget (src/api/telegram_login.py)."""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from src.api.telegram_login import LoginAuthError, verify_login_widget


BOT_TOKEN = "123456:LOGIN_WIDGET_TEST_TOKEN"


def _sign(data: dict, *, bot_token: str = BOT_TOKEN) -> dict:
    """Подписывает поля как настоящий Login Widget (secret = sha256(token))."""
    pairs = {k: ("" if v is None else str(v)) for k, v in data.items()}
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**pairs, "hash": h}


def test_valid_login():
    data = _sign({
        "id": 555, "first_name": "Иван", "username": "ivan",
        "photo_url": "https://t.me/i/x.jpg",
        "auth_date": int(time.time()),
    })
    user = verify_login_widget(data, bot_token=BOT_TOKEN, max_age_seconds=3600)
    assert user.id == 555
    assert user.username == "ivan"
    assert user.first_name == "Иван"


def test_bad_hash_rejected():
    data = _sign({"id": 1, "auth_date": int(time.time())})
    data["hash"] = "deadbeef" * 8
    with pytest.raises(LoginAuthError):
        verify_login_widget(data, bot_token=BOT_TOKEN, max_age_seconds=3600)


def test_wrong_bot_token_rejected():
    data = _sign({"id": 1, "auth_date": int(time.time())})
    with pytest.raises(LoginAuthError):
        verify_login_widget(data, bot_token="999:OTHER", max_age_seconds=3600)


def test_expired_rejected():
    data = _sign({"id": 1, "auth_date": int(time.time()) - 99999})
    with pytest.raises(LoginAuthError):
        verify_login_widget(data, bot_token=BOT_TOKEN, max_age_seconds=3600)


def test_missing_hash_rejected():
    with pytest.raises(LoginAuthError):
        verify_login_widget(
            {"id": 1, "auth_date": str(int(time.time()))},
            bot_token=BOT_TOKEN, max_age_seconds=3600,
        )
