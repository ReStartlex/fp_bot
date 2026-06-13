"""
Расчёт прибыли по выполненному заказу (Decimal, из уже известных данных).

Проверяем:
  1. compute_profit_breakdown — формула на реальном примере (XT8EZFFE)
     и None при нехватке/невалидности данных;
  2. order_financials — сводки СУММИРУЮТ сохранённый profit_rub, а не
     пересчитывают по текущему курсу; fallback для старых заказов;
  3. order_success показывает «Прибыль: X₽» / «n/a», выдачу не ломает.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.alerts.telegram import TelegramNotifier
from src.mapping.rules import compute_profit_breakdown, order_financials


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x", ns_api_secret="QQ==",
        funpay_golden_key="x", funpay_user_id=1,
        telegram_bot_token=None, telegram_use_proxy=False,
    )


# ───────────── compute_profit_breakdown ─────────────

def test_breakdown_real_example_xt8ezffe():
    b = compute_profit_breakdown(
        sold_rub=918.0,
        ns_price_usd=11.5769,
        usd_rub_rate_at_sale=75.5031,
        fee_rate=0.03,
    )
    assert b is not None
    assert b.sold_rub == Decimal("918.00")
    assert b.cost_rub == Decimal("874.09")       # 11.5769 * 75.5031
    assert b.funpay_fee_rub == Decimal("27.54")  # 918 * 0.03
    assert b.profit_rub == Decimal("16.37")      # 918 - 27.54 - 874.09
    assert b.usd_rub_rate_at_sale == Decimal("75.5031")


def test_breakdown_none_when_data_missing_or_invalid():
    base = dict(ns_price_usd=11.5769, usd_rub_rate_at_sale=75.5031, fee_rate=0.03)
    assert compute_profit_breakdown(sold_rub=None, **base) is None
    assert compute_profit_breakdown(
        sold_rub=100, ns_price_usd=None, usd_rub_rate_at_sale=75.5, fee_rate=0.03
    ) is None
    assert compute_profit_breakdown(
        sold_rub=100, ns_price_usd=1, usd_rub_rate_at_sale=0, fee_rate=0.03
    ) is None
    assert compute_profit_breakdown(
        sold_rub=0, ns_price_usd=1, usd_rub_rate_at_sale=75.5, fee_rate=0.03
    ) is None


def test_breakdown_zero_ns_cost_is_full_profit_minus_fee():
    b = compute_profit_breakdown(
        sold_rub=100.0, ns_price_usd=0.0, usd_rub_rate_at_sale=75.0, fee_rate=0.03
    )
    assert b is not None
    assert b.cost_rub == Decimal("0.00")
    assert b.funpay_fee_rub == Decimal("3.00")
    assert b.profit_rub == Decimal("97.00")


# ───────────── order_financials (сводки) ─────────────

def test_financials_prefers_stored_profit_not_recomputed():
    order = SimpleNamespace(
        funpay_price_rub=918.0, ns_price_usd=11.5769, fx_rate_at_sale=75.5031,
        profit_rub=16.37, cost_rub=874.09, funpay_fee_rub=27.54,
    )
    # fallback_fx намеренно абсурдный — НЕ должен использоваться
    rev, cost, fee, profit = order_financials(
        order, fallback_fx=999.0, withdrawal_fee_percent=3.0
    )
    assert profit == 16.37
    assert rev == 918.0
    assert cost == 874.09
    assert fee == 27.54


def test_financials_fallback_recompute_uses_stored_fx_not_current():
    # Старый заказ: profit_rub не сохранён → пересчёт по СОХРАНЁННОМУ fx,
    # а не по текущему (fallback_fx).
    order = SimpleNamespace(
        funpay_price_rub=918.0, ns_price_usd=11.5769, fx_rate_at_sale=75.5031,
        profit_rub=None, cost_rub=None, funpay_fee_rub=None,
    )
    fin = order_financials(order, fallback_fx=999.0, withdrawal_fee_percent=3.0)
    assert fin is not None
    _rev, cost, _fee, profit = fin
    assert abs(profit - 16.37) < 0.5
    assert abs(cost - 874.09) < 0.5  # по 75.5031, не по 999


def test_financials_none_when_no_data():
    order = SimpleNamespace(
        funpay_price_rub=None, ns_price_usd=None, fx_rate_at_sale=None,
        profit_rub=None, cost_rub=None, funpay_fee_rub=None,
    )
    assert order_financials(
        order, fallback_fx=80.0, withdrawal_fee_percent=3.0
    ) is None


# ───────────── order_success notification ─────────────

@pytest.mark.asyncio
async def test_order_success_shows_profit(monkeypatch):
    sent: list[str] = []
    n = TelegramNotifier(_settings())

    async def _capture(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(n, "send", _capture)
    await n.order_success(
        funpay_order_id="XT8EZFFE", ns_custom_id="ns-1",
        ns_price_usd=11.5769, funpay_price_rub=918.0,
        buyer_username="sigma300013", profit_rub=16.37,
    )
    assert "Прибыль: 16.37₽" in sent[0]
    assert "sigma300013" in sent[0]


@pytest.mark.asyncio
async def test_order_success_profit_na_when_none(monkeypatch):
    sent: list[str] = []
    n = TelegramNotifier(_settings())

    async def _capture(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(n, "send", _capture)
    await n.order_success(
        funpay_order_id="X", ns_custom_id="n",
        ns_price_usd=1.0, funpay_price_rub=None,
        buyer_username=None, profit_rub=None,
    )
    assert "Прибыль: n/a" in sent[0]
