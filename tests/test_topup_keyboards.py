"""
Тесты клавиатур top-up flow.
"""
from __future__ import annotations

import pytest

from src.shop.keyboards import (
    TOPUP_PRESET_AMOUNTS_RUB,
    topup_amount_keyboard,
    topup_custom_amount_prompt,
    topup_invoice_keyboard,
)


# ─── amount selection ───────────────────────────────────────────────


def test_topup_amount_kb_has_all_presets():
    text, kb = topup_amount_keyboard(min_rub=100, max_rub=100000)
    flat = [b for row in kb.inline_keyboard for b in row]
    callbacks = [b.callback_data for b in flat]
    # Каждая предустановка с callback tp_amt:{kopecks}
    for amount in TOPUP_PRESET_AMOUNTS_RUB:
        assert f"tp_amt:{amount * 100}" in callbacks
    # Своя сумма всегда есть
    assert "tp_amt:custom" in callbacks
    # Кнопка возврата
    assert "bal" in callbacks


def test_topup_amount_kb_filters_by_min():
    """Если min_rub=500, кнопки 100/300 не показываем."""
    text, kb = topup_amount_keyboard(min_rub=500, max_rub=10000)
    flat_cb = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "tp_amt:10000" not in flat_cb  # 100 ₽
    assert "tp_amt:30000" not in flat_cb  # 300 ₽
    assert "tp_amt:50000" in flat_cb  # 500 ₽
    assert "tp_amt:100000" in flat_cb  # 1000 ₽


def test_topup_amount_kb_text_mentions_limits():
    text, kb = topup_amount_keyboard(min_rub=100, max_rub=50000)
    assert "100" in text
    assert "50 000" in text or "50000" in text


def test_topup_amount_kb_three_two_layout():
    """Первый ряд — 3 кнопки, второй ряд — оставшиеся 2."""
    text, kb = topup_amount_keyboard(min_rub=100, max_rub=100000)
    rows = kb.inline_keyboard
    # rows[0] — 3 первые предустановки
    assert len(rows[0]) == 3
    # rows[1] — 2 оставшиеся
    assert len(rows[1]) == 2


# ─── invoice card ───────────────────────────────────────────────────


def test_topup_invoice_kb_contains_pay_url():
    text, kb = topup_invoice_keyboard(
        amount_kopecks=50000,
        pay_url="https://t.me/CryptoBot?start=I_xxx",
        invoice_id=123,
    )
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert any("t.me/CryptoBot" in u for u in urls)


def test_topup_invoice_kb_check_status_button():
    text, kb = topup_invoice_keyboard(
        amount_kopecks=10000, pay_url="https://x", invoice_id=999,
    )
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row
                 if b.callback_data]
    assert "tp_check:999" in callbacks
    assert "tp_cancel:999" in callbacks


def test_topup_invoice_kb_text_shows_amount_and_id():
    text, _ = topup_invoice_keyboard(
        amount_kopecks=50050, pay_url="x", invoice_id=42,
    )
    assert "500,50" in text or "500.50" in text
    assert "#42" in text


# ─── custom amount prompt ───────────────────────────────────────────


def test_custom_amount_prompt_mentions_limits():
    prompt = topup_custom_amount_prompt(min_rub=100, max_rub=100000)
    assert "100" in prompt
    assert "100 000" in prompt
