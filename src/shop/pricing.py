"""
Расчёт shop-цены в копейках Integer.

Чистая функция без I/O — её можно гонять как unit-тест миллион раз
без поднятия БД/HTTP. Все валидации тут же, чтобы плохие значения
никогда не попадали в shop_catalog_cache.

Формула:
    price_rub_kopecks = round(ns_price_usd * fx_rate * (1 + markup/100) * 100)

Чем отличается от mapping.rules.compute_pricing (FunPay):
- Нет funpay_commission_percent (FunPay добавляет её сверху сам).
- Нет stock_cap (NS сам ограничит при покупке; в shop'е мы продаём
  ровно столько, сколько NS реально может выдать).
- Нет funpay_withdrawal_fee (это потеря при выводе с FunPay, у shop'а
  она нулевая — CryptoBot/Stars выплачивают на наш кошелёк напрямую).
"""
from __future__ import annotations


def compute_shop_price_kopecks(
    *,
    ns_price_usd: float,
    fx_rate: float,
    markup_percent: float,
) -> int:
    """
    Цена для покупателя в копейках Integer. Округление до ближайшей
    копейки (round-half-even, как у Python `round`).

    Raises:
        ValueError: если ns_price_usd <= 0, fx_rate <= 0,
                    или markup_percent < 0.
    """
    if ns_price_usd <= 0:
        raise ValueError(f"ns_price_usd must be > 0, got {ns_price_usd}")
    if fx_rate <= 0:
        raise ValueError(f"fx_rate must be > 0, got {fx_rate}")
    if markup_percent < 0:
        raise ValueError(
            f"markup_percent must be >= 0, got {markup_percent}"
        )

    price_rub = ns_price_usd * fx_rate * (1.0 + markup_percent / 100.0)
    # Multiply on rubles, THEN round to kopecks. Это даёт стабильное
    # округление, не зависящее от двух последовательных round().
    price_kopecks = int(round(price_rub * 100))
    return max(0, price_kopecks)
