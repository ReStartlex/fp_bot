"""
Тесты shop-pricing — чистая функция без I/O.

Формула (без FunPay-комиссии и withdrawal-fee, эти расходы у нас нулевые):

    price_rub_kopecks = round(ns_price_usd * fx_rate * (1 + markup/100) * 100)

Деньги в копейках Integer навсегда, никаких float-значений ниже копейки
никуда не утекает — это база финансовой целостности shop'а.
"""
from __future__ import annotations

import pytest

from src.shop.pricing import compute_shop_price_kopecks


def test_basic_pricing():
    """5$ × 90₽ × 1.08 = 486₽ = 48600 копеек."""
    assert compute_shop_price_kopecks(
        ns_price_usd=5.0, fx_rate=90.0, markup_percent=8.0
    ) == 48600


def test_rounding_to_nearest_kopeck():
    """1.23$ × 73.50₽ × 1.08 = 97.6293₽ ≈ 97.63₽ = 9763 копейки."""
    result = compute_shop_price_kopecks(
        ns_price_usd=1.23, fx_rate=73.50, markup_percent=8.0
    )
    # 1.23 * 73.50 * 1.08 = 97.6446 → 9764
    assert result == 9764


def test_zero_markup_passes_through():
    """0% наценка = чистая конвертация."""
    assert compute_shop_price_kopecks(
        ns_price_usd=10.0, fx_rate=80.0, markup_percent=0.0
    ) == 80000  # 10 * 80 * 1.0 = 800₽ = 80000 коп


def test_high_markup():
    """100% наценка удваивает цену."""
    assert compute_shop_price_kopecks(
        ns_price_usd=1.0, fx_rate=100.0, markup_percent=100.0
    ) == 20000  # 1 * 100 * 2 = 200₽


def test_small_amount_does_not_lose_precision():
    """0.01$ × 80₽ × 1.08 = 0.864₽ = 86 копеек (round-half-even)."""
    result = compute_shop_price_kopecks(
        ns_price_usd=0.01, fx_rate=80.0, markup_percent=8.0
    )
    assert result == 86


def test_rejects_zero_price():
    """NS-услуга с ценой 0 — не должна попадать в каталог shop'а."""
    with pytest.raises(ValueError, match="ns_price_usd"):
        compute_shop_price_kopecks(
            ns_price_usd=0.0, fx_rate=90.0, markup_percent=8.0
        )


def test_rejects_negative_price():
    with pytest.raises(ValueError, match="ns_price_usd"):
        compute_shop_price_kopecks(
            ns_price_usd=-1.0, fx_rate=90.0, markup_percent=8.0
        )


def test_rejects_zero_fx():
    with pytest.raises(ValueError, match="fx_rate"):
        compute_shop_price_kopecks(
            ns_price_usd=5.0, fx_rate=0.0, markup_percent=8.0
        )


def test_rejects_negative_markup():
    """Отрицательный markup = продаём ниже себестоимости. Не допускаем."""
    with pytest.raises(ValueError, match="markup_percent"):
        compute_shop_price_kopecks(
            ns_price_usd=5.0, fx_rate=90.0, markup_percent=-1.0
        )


def test_zero_markup_allowed():
    """0% — допустимая граница (промо/акция при ручной установке)."""
    assert compute_shop_price_kopecks(
        ns_price_usd=5.0, fx_rate=90.0, markup_percent=0.0
    ) == 45000


def test_realistic_apple_giftcard_5usd():
    """Apple Gift Card $5 при USD/RUB=73.35 (ЦБ+3%), markup=8%."""
    # 5 * 73.35 * 1.08 = 396.09₽
    result = compute_shop_price_kopecks(
        ns_price_usd=5.0, fx_rate=73.35, markup_percent=8.0
    )
    assert result == 39609
