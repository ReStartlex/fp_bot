"""Тесты логики расчёта цены и стока."""
from __future__ import annotations

import pytest

from src.config import Currency
from src.db.models import Mapping
from src.mapping.rules import compute_pricing, should_update_price
from src.ns.models import Service


class _S:
    """Минимальный stub Settings для тестов цен."""
    markup_percent = 15.0
    funpay_currency = Currency.RUB
    funpay_stock_cap = 50
    funpay_commission_percent = 12.5


def _svc(price: float = 2.0, in_stock: int = 100) -> Service:
    return Service(
        service_id=1,
        service_name="Test",
        price=price,
        currency="USD",
        in_stock=in_stock,
    )


def _map(markup: float | None = None, cap: int | None = None) -> Mapping:
    m = Mapping(funpay_lot_id=1, ns_service_id=1)
    m.markup_percent = markup
    m.stock_cap = cap
    m.enabled = True
    return m


def test_pricing_basic_rub():
    res = compute_pricing(
        ns_service=_svc(price=2.0, in_stock=100),
        mapping=_map(),
        settings=_S(),  # type: ignore[arg-type]
        fx_rate_usd_to_target=90.0,
    )
    # 2 USD * 90 RUB/USD * 1.15 = 207
    assert res.price_target == pytest.approx(207.0)
    assert res.round_price() == 207
    assert res.stock == 50  # cap из settings
    assert res.currency == Currency.RUB


def test_pricing_mapping_overrides_markup():
    res = compute_pricing(
        ns_service=_svc(price=2.0),
        mapping=_map(markup=20.0),
        settings=_S(),  # type: ignore[arg-type]
        fx_rate_usd_to_target=90.0,
    )
    assert res.price_target == pytest.approx(2 * 90 * 1.20)
    assert res.markup_percent == 20.0


def test_pricing_stock_cap_override():
    res = compute_pricing(
        ns_service=_svc(in_stock=10),
        mapping=_map(cap=200),
        settings=_S(),  # type: ignore[arg-type]
        fx_rate_usd_to_target=90.0,
    )
    assert res.stock == 10  # упирается в NS, а не в cap


def test_pricing_stock_zero_when_ns_empty():
    res = compute_pricing(
        ns_service=_svc(in_stock=0),
        mapping=_map(),
        settings=_S(),  # type: ignore[arg-type]
        fx_rate_usd_to_target=90.0,
    )
    assert res.stock == 0


def test_pricing_usd_target_skips_fx():
    class S(_S):
        funpay_currency = Currency.USD

    res = compute_pricing(
        ns_service=_svc(price=2.0),
        mapping=_map(),
        settings=S(),  # type: ignore[arg-type]
        fx_rate_usd_to_target=90.0,  # должен игнорироваться
    )
    assert res.price_target == pytest.approx(2.0 * 1.15)
    assert res.fx_rate == 1.0


def test_pricing_client_price_with_commission():
    """Цена клиента = цена продавца / (1 - commission/100)."""
    class S(_S):
        funpay_commission_percent = 12.5

    res = compute_pricing(
        ns_service=_svc(price=2.0),
        mapping=_map(),
        settings=S(),  # type: ignore[arg-type]
        fx_rate_usd_to_target=90.0,
    )
    # seller = 207, client = 207 / 0.875 ≈ 236.57
    assert res.price_target == pytest.approx(207.0)
    assert res.client_price == pytest.approx(207.0 / 0.875)


def test_pricing_zero_commission_means_equal():
    class S(_S):
        funpay_commission_percent = 0.0

    res = compute_pricing(
        ns_service=_svc(price=2.0),
        mapping=_map(),
        settings=S(),  # type: ignore[arg-type]
        fx_rate_usd_to_target=90.0,
    )
    assert res.client_price == pytest.approx(res.price_target)


def test_should_update_price_first_time():
    assert should_update_price(None, 100.0, threshold_percent=2.0) is True


def test_should_update_price_visible_unit_change_even_below_threshold():
    # 100 -> 101 = +1% ниже порога 2%, но это видимое изменение цены.
    # Ручной markup должен реально применяться, а не застревать на updated=0.
    assert should_update_price(100.0, 101.0, threshold_percent=2.0) is True


def test_should_update_price_sub_unit_noise_below_threshold():
    # Для дробных валют/шумовых колебаний меньше единицы порог всё ещё работает.
    assert should_update_price(100.0, 100.5, threshold_percent=2.0) is False


def test_should_update_price_above_threshold():
    # 100 -> 103 = +3%
    assert should_update_price(100.0, 103.0, threshold_percent=2.0) is True


def test_should_update_price_zero_old():
    assert should_update_price(0.0, 50.0, threshold_percent=2.0) is True


def test_pricing_fractional_markup_propagates_through_calculation():
    """Дробная наценка 5.5% должна влиять на цену с двумя знаками точности."""
    res = compute_pricing(
        ns_service=_svc(price=2.0),
        mapping=_map(markup=5.5),
        settings=_S(),  # type: ignore[arg-type]
        fx_rate_usd_to_target=90.0,
    )
    assert res.markup_percent == pytest.approx(5.5)
    # 2 * 90 * 1.055 = 189.9
    assert res.price_target == pytest.approx(189.9)
    assert res.round_price() == 190
