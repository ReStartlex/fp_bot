"""Тесты подписанных session-токенов сайта (src/api/web_session.py)."""
from __future__ import annotations

import time

import pytest

from src.api.web_session import (
    SessionError,
    issue_session_token,
    verify_session_token,
)
from src.config import Settings


def _settings(**over) -> Settings:
    base = dict(
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="YWJj",  # base64 "abc"
        funpay_golden_key="x" * 10, funpay_user_id=1,
        shop_telegram_bot_token="123:ABCDEF",
    )
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_roundtrip():
    s = _settings()
    tok = issue_session_token(user_id=7, telegram_user_id=4242, settings=s)
    claims = verify_session_token(tok, settings=s)
    assert claims.user_id == 7
    assert claims.telegram_user_id == 4242


def test_tamper_breaks_signature():
    s = _settings()
    tok = issue_session_token(user_id=7, telegram_user_id=4242, settings=s)
    head, _, _sig = tok.partition(".")
    forged = head + ".AAAA"
    with pytest.raises(SessionError):
        verify_session_token(forged, settings=s)


def test_expired_rejected():
    s = _settings(site_session_ttl_seconds=3600)
    past = time.time() - 10_000
    tok = issue_session_token(
        user_id=7, telegram_user_id=4242, settings=s, now=past,
    )
    with pytest.raises(SessionError):
        verify_session_token(tok, settings=s)


def test_different_secret_rejected():
    s1 = _settings(web_session_secret="secret-one")
    s2 = _settings(web_session_secret="secret-two")
    tok = issue_session_token(user_id=1, telegram_user_id=2, settings=s1)
    with pytest.raises(SessionError):
        verify_session_token(tok, settings=s2)


def test_explicit_secret_independent_of_bot_token():
    """С явным web_session_secret смена bot-токена не ломает сессии."""
    s1 = _settings(web_session_secret="fixed", shop_telegram_bot_token="111:AAA")
    s2 = _settings(web_session_secret="fixed", shop_telegram_bot_token="222:BBB")
    tok = issue_session_token(user_id=5, telegram_user_id=9, settings=s1)
    assert verify_session_token(tok, settings=s2).user_id == 5
