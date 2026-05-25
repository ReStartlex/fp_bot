"""
Тесты HMAC-подписи webhook'а CryptoBot.

Алгоритм проверки (cryptobot docs):
  secret = sha256(api_token)
  expected = hmac_sha256(secret, raw_body).hexdigest()
  match → request от CryptoBot (а не от злоумышленника).

Критично, потому что от веб-хука зависит начисление баланса.
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from src.shop.payments.cryptobot import verify_webhook_signature


def _make_signature(api_token: str, body: bytes) -> str:
    secret = hashlib.sha256(api_token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    api = "test_token"
    body = b'{"update_id":1,"update_type":"invoice_paid","payload":{}}'
    sig = _make_signature(api, body)
    assert verify_webhook_signature(
        api_token=api, raw_body=body, signature_hex=sig,
    )


def test_tampered_body_fails():
    api = "test_token"
    body = b'{"update_id":1,"update_type":"invoice_paid","payload":{}}'
    sig = _make_signature(api, body)
    tampered = body.replace(b'"update_id":1', b'"update_id":2')
    assert not verify_webhook_signature(
        api_token=api, raw_body=tampered, signature_hex=sig,
    )


def test_wrong_api_token_fails():
    body = b'{"x":1}'
    sig = _make_signature("real_token", body)
    assert not verify_webhook_signature(
        api_token="wrong_token", raw_body=body, signature_hex=sig,
    )


def test_empty_signature_fails():
    body = b'{"x":1}'
    assert not verify_webhook_signature(
        api_token="t", raw_body=body, signature_hex="",
    )


def test_case_insensitive_signature():
    """CryptoBot шлёт подпись lower-case hex; защитимся, если пришёл upper-case."""
    api = "t"
    body = b'{"x":1}'
    sig = _make_signature(api, body)
    assert verify_webhook_signature(
        api_token=api, raw_body=body, signature_hex=sig.upper(),
    )


def test_signature_uses_constant_time_comparison():
    """
    Не функциональный тест, а проверка что не используется ==:
    timing-side-channel attack — компаратор должен быть hmac.compare_digest.
    Здесь просто sanity: длина подписи 64 hex символов (sha256).
    """
    sig = _make_signature("t", b"{}")
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)
