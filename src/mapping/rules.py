"""Логика расчёта итоговой цены и стока для FunPay по данным NS."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from src.config import Currency, Settings
from src.db.models import Mapping
from src.ns.models import Service


_CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """Округление денежной величины до копеек (банковское HALF_UP)."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


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
    default_markup: float | None = None,
    default_stock_cap: int | None = None,
    group_markup_percent: float | None = None,
    group_stock_cap: int | None = None,
) -> PricingResult:
    """
    Рассчитать что нужно выставить на FunPay для данного NS service + mapping.

    Приоритет наценки:
        1) mapping.markup_percent (если не NULL — явная индивидуальная)
        2) default_markup (runtime override, переданный сверху)
        3) settings.markup_percent (из .env)

    То же самое для stock_cap.
    """
    if mapping.markup_percent is not None:
        markup = mapping.markup_percent
    elif group_markup_percent is not None:
        markup = group_markup_percent
    elif default_markup is not None:
        markup = default_markup
    else:
        markup = settings.markup_percent

    if mapping.stock_cap is not None:
        stock_cap = mapping.stock_cap
    elif group_stock_cap is not None:
        stock_cap = group_stock_cap
    elif default_stock_cap is not None:
        stock_cap = default_stock_cap
    else:
        stock_cap = settings.funpay_stock_cap

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
    True если новая цена должна быть записана на FunPay.

    Для RUB-лотов важен сам факт видимого изменения цены: если цена на витрине
    должна стать 145 вместо 147, её надо обновить даже при пороге 2%, иначе
    ручная смена markup 7% -> 5.5% выглядит "нерабочей".

    threshold_percent остаётся защитой от мелкого шума для дробных валют и
    sub-unit колебаний.
    Если старая неизвестна — всегда True.
    """
    if old_price is None or old_price <= 0:
        return True
    if abs(new_price - old_price) >= 1.0:
        return True
    diff_percent = abs(new_price - old_price) / old_price * 100.0
    return diff_percent >= threshold_percent


def estimate_profit_rub(
    funpay_price_rub: float | None,
    ns_price_usd: float | None,
    fx_rate: float,
    *,
    withdrawal_fee_percent: float = 3.0,
) -> tuple[float, float, float, float] | None:
    """
    Оценка прибыли по заказу в RUB: revenue, ns_cost, net_profit, margin%.

    `revenue` — сумма продажи на FunPay до вывода.
    `net_profit` уже учитывает потерю на выводе с FunPay.
    """
    if funpay_price_rub is None or ns_price_usd is None:
        return None
    if (
        funpay_price_rub <= 0
        or ns_price_usd < 0
        or fx_rate <= 0
        or withdrawal_fee_percent < 0
    ):
        return None
    cost_rub = ns_price_usd * fx_rate
    withdrawal_fee_rub = funpay_price_rub * withdrawal_fee_percent / 100.0
    profit_rub = funpay_price_rub - withdrawal_fee_rub - cost_rub
    margin_percent = profit_rub / funpay_price_rub * 100.0
    return funpay_price_rub, cost_rub, profit_rub, margin_percent


@dataclass(frozen=True)
class ProfitBreakdown:
    """
    Разложение прибыли по выполненному заказу (в RUB), деньги — Decimal.

    profit_rub = sold_rub - funpay_fee_rub - cost_rub
      cost_rub      = ns_price_usd * usd_rub_rate_at_sale
      funpay_fee_rub = sold_rub * fee_rate (комиссия вывода с FunPay)
    """
    sold_rub: Decimal
    cost_rub: Decimal
    funpay_fee_rub: Decimal
    profit_rub: Decimal
    margin_percent: Decimal
    usd_rub_rate_at_sale: Decimal


def compute_profit_breakdown(
    *,
    sold_rub,
    ns_price_usd,
    usd_rub_rate_at_sale,
    fee_rate,
) -> ProfitBreakdown | None:
    """
    Прибыль по заказу из УЖЕ ИЗВЕСТНЫХ данных (без внешних запросов).
    Decimal на всех денежных шагах. None — если не хватает данных или
    значения невалидны (вызывающий покажет «n/a», выдачу не ломаем).

    Параметры — что угодно, приводимое к Decimal (float/str/Decimal):
      sold_rub               — цена продажи на FunPay (RUB);
      ns_price_usd           — сколько списал NS (USD);
      usd_rub_rate_at_sale   — курс USD→RUB на момент продажи;
      fee_rate               — доля комиссии вывода FunPay (0.03 = 3%).
    """
    sold = _to_decimal(sold_rub)
    ns_usd = _to_decimal(ns_price_usd)
    rate = _to_decimal(usd_rub_rate_at_sale)
    fee = _to_decimal(fee_rate)
    if sold is None or ns_usd is None or rate is None or fee is None:
        return None
    if sold <= 0 or ns_usd < 0 or rate <= 0 or fee < 0:
        return None

    cost = _money(ns_usd * rate)
    fee_rub = _money(sold * fee)
    profit = _money(sold - fee_rub - cost)
    margin = (profit / sold * Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return ProfitBreakdown(
        sold_rub=_money(sold),
        cost_rub=cost,
        funpay_fee_rub=fee_rub,
        profit_rub=profit,
        margin_percent=margin,
        usd_rub_rate_at_sale=rate,
    )


def order_financials(
    order,
    *,
    fallback_fx: float,
    withdrawal_fee_percent: float,
) -> tuple[float, float, float, float] | None:
    """
    (revenue, cost, fee, profit) в RUB для одного заказа в СВОДКАХ.

    Приоритет — СОХРАНЁННЫЕ при доставке значения: дневная/недельная/
    месячная прибыль суммируется из `order.profit_rub`, а НЕ пересчитывается
    задним числом по текущему курсу. Для старых заказов без сохранённого
    `profit_rub` — fallback-пересчёт по сохранённому `fx_rate_at_sale`
    (или `fallback_fx`, если и его нет). None — если данных не хватает.
    """
    sold = getattr(order, "funpay_price_rub", None)
    stored_profit = getattr(order, "profit_rub", None)
    if stored_profit is not None and sold is not None:
        cost = getattr(order, "cost_rub", None)
        if cost is None:
            ns_usd = getattr(order, "ns_price_usd", None) or 0.0
            fx = getattr(order, "fx_rate_at_sale", None) or fallback_fx
            cost = ns_usd * fx
        fee = getattr(order, "funpay_fee_rub", None)
        if fee is None:
            fee = sold * withdrawal_fee_percent / 100.0
        return float(sold), float(cost), float(fee), float(stored_profit)

    # Старый заказ без сохранённого профита — пересчитываем по сохранённому fx.
    fx = getattr(order, "fx_rate_at_sale", None) or fallback_fx
    est = estimate_profit_rub(
        sold,
        getattr(order, "ns_price_usd", None),
        fx,
        withdrawal_fee_percent=withdrawal_fee_percent,
    )
    if est is None:
        return None
    revenue, cost, profit, _ = est
    fee = revenue * withdrawal_fee_percent / 100.0
    return revenue, cost, fee, profit
