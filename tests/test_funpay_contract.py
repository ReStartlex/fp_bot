"""
Contract-тесты разбора ответов FunPay /lots/offerSave (P0-2).

Фиксируют поведение classify_offersave_response на РЕАЛЬНЫХ ответах
FunPay (tests/fixtures/funpay/*), наблюдавшихся в проде. Парсер уже
дважды давал тихий ложный «успех» (amount=0; цена вне диапазона) —
эти тесты не дадут рефактору (в т.ч. выпилу FunPayAPI) вернуть регресс.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.funpay.admin_http import classify_offersave_response

FIXTURES = Path(__file__).parent / "fixtures" / "funpay"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ───────────── успех ─────────────

def test_offersave_ok_done_true():
    res = classify_offersave_response(
        status_code=200, text=_load("offersave_ok.json"),
        content_type="application/json",
    )
    assert res["ok"] is True
    assert "funpay_error" not in res


def test_offersave_ok_legacy_msg():
    res = classify_offersave_response(
        status_code=200, text=_load("offersave_msg_ok.json"),
    )
    assert res["ok"] is True


# ───────────── валидационные ошибки (msg пустой!) ─────────────

def test_offersave_error_price_is_failure():
    res = classify_offersave_response(
        status_code=200, text=_load("offersave_error_price.json"),
    )
    assert res["ok"] is False
    # ошибка вытащена из errors, а не из (пустого) msg
    assert "price" in str(res["funpay_error"])


def test_offersave_error_field_is_failure():
    res = classify_offersave_response(
        status_code=200, text=_load("offersave_error_field.json"),
    )
    assert res["ok"] is False
    assert "fields[try]" in str(res["funpay_error"])


def test_offersave_error_flag_only():
    """error=1 без errors — тоже провал (инцидент amount=0)."""
    res = classify_offersave_response(
        status_code=200, text='{"error": 1}',
    )
    assert res["ok"] is False


# ───────────── 429 ─────────────

def test_offersave_429_is_failure():
    res = classify_offersave_response(
        status_code=429, text="", retry_after="3", max_429_retries=4,
    )
    assert res["ok"] is False
    assert "429" in str(res["funpay_error"])
    assert "5 попыток" in str(res["funpay_error"])  # max+1


# ───────────── HTML: логин-редирект = протухла сессия ─────────────

def test_offersave_login_redirect_is_failure():
    res = classify_offersave_response(
        status_code=200, text=_load("login_redirect.html"),
        content_type="text/html",
    )
    assert res["ok"] is False
    assert "golden_key" in str(res["funpay_error"]) or "логин" in str(res["funpay_error"]).lower()


def test_offersave_html_error_marker_is_failure():
    res = classify_offersave_response(
        status_code=200, text="<html><body>Произошла ошибка</body></html>",
    )
    assert res["ok"] is False


def test_offersave_html_500_is_failure():
    res = classify_offersave_response(
        status_code=500, text="<html>Internal Server Error</html>",
    )
    assert res["ok"] is False


def test_offersave_html_302_redirect_ok():
    """3xx-редирект (на страницу лота) без маркеров логина/ошибки — успех."""
    res = classify_offersave_response(
        status_code=302, text="", content_type="text/html",
    )
    assert res["ok"] is True


# ───────────── мусор ─────────────

def test_offersave_garbage_is_failure():
    res = classify_offersave_response(status_code=200, text="!!! not json !!!")
    assert res["ok"] is False
