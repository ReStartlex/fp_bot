"""Логика расчёта итоговой цены и стока для FunPay по данным NS."""
from __future__ import annotations

from dataclasses import dataclass

from src.config import Currency, Settings
from src.db.models import Mapping
from src.ns.models import Service


@dataclass
class PricingResult:
    """Результат расчёта цены для одного лота."""
    ns_price_usd: float
    fx_rate: float                  # курс USD -> целевая валюта
    markup_percent: float
    price_target: float             # цена продавца (то что мы получим), в валюте FunPay
    stock: int                      # сколько шт показывать на FunPay
    currency: Currency
    commission_percent: float = 0.0
    client_price: float = 0.0       # оценка цены клиента с комиссией FunPay

    def round_price(self) -> float:
        """Округление цены продавца: для RUB до целого, для USD/EUR до .01."""
        if self.currency == Currency.RUB:
            return round(self.price_target)
        return round(self.price_target, 2)

    def round_client_price(self) -> float:
        if self.currency == Currency.RUB:
            return round(self.client_price)
        return round(self.client_price, 2)


def compute_pricing(
    *,
    ns_service: Service,
    mapping: Mapping,
    settings: Settings,
    fx_rate_usd_to_target: float,
) -> PricingResult:
    """
    Рассчитать что нужно выставить на FunPay для данного NS service + mapping.

    - markup берётся из mapping (если задан) или глобальный из settings
    - stock_cap из mapping (если задан) или глобальный
    """
    markup = mapping.markup_percent if mapping.markup_percent is not None else settings.markup_percent
    stock_cap = mapping.stock_cap if mapping.stock_cap is not None else settings.funpay_stock_cap

    ns_price = ns_service.price  # USD
    # Конверсия + наценка
    if settings.funpay_currency == Currency.USD:
        price_target = ns_price * (1.0 + markup / 100.0)
        fx = 1.0
    else:
        price_target = ns_price * fx_rate_usd_to_target * (1.0 + markup / 100.0)
        fx = fx_rate_usd_to_target

    stock = max(0, min(ns_service.in_stock, stock_cap))

    commission = settings.funpay_commission_percent
    # client_price = seller_price / (1 - commission/100): FunPay добавляет комиссию сверху
    if commission >= 99.0:
        client_price = price_target
    else:
        client_price = price_target / (1.0 - commission / 100.0)

    return PricingResult(
        ns_price_usd=ns_price,
        fx_rate=fx,
        markup_percent=markup,
        price_target=price_target,
        stock=stock,
        currency=settings.funpay_currency,
        commission_percent=commission,
        client_price=client_price,
    )


def should_update_price(
    old_price: float | None,
    new_price: float,
    threshold_percent: float,
) -> bool:
    """
    True если новая цена отличается от старой больше чем на threshold%.
    Если старая неизвестна — всегда True.
    """
    if old_price is None or old_price <= 0:
        return True
    diff_percent = abs(new_price - old_price) / old_price * 100.0
    return diff_percent >= threshold_percent
