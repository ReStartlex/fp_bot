"""
Sprint 5 — checkout flow для shop-бота.

Эта точка — единственный путь от карточки товара к paid-заказу.
Сюда сходятся все проверки наличия, баланса, цены — и здесь же
происходит атомарное создание ShopOrder + дебит баланса.

Контракт:
  * Никаких side-effect'ов кроме БД — никаких Telegram-отправок,
    NS-запросов, логов уровня бизнес-аналитики. Это pure repo-layer.
  * Атомарность через session: либо ShopOrder создан И balance дебитован,
    либо ничего (исключения откатываются вызывающим).
  * Идемпотентность — здесь нет, она на уровне delivery/cashback.

Жизненный цикл, который этот модуль покрывает:
  draft → paid
Дальше — `src/shop/delivery.py`:
  paid → delivering → delivered (или failed → refunded)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ShopCatalogCache, ShopOrder, ShopUser
from src.shop.repo import (
    LEDGER_REASON_ORDER_PAYMENT,
    apply_balance_change,
    create_shop_order,
    get_catalog_service,
    mark_order_paid,
)


class CheckoutOutcome(str, Enum):
    """Все возможные исходы checkout'а — в дискриминированном виде."""
    OK = "ok"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    OUT_OF_STOCK = "out_of_stock"
    SERVICE_DISABLED = "service_disabled"
    SERVICE_NOT_FOUND = "service_not_found"
    REQUIRES_FIELDS = "requires_fields"  # Sprint 5.1: для услуг с email/username
    USER_BLOCKED = "user_blocked"


@dataclass
class CheckoutResult:
    """
    Результат checkout-операции.

    Поля заполнены избирательно в зависимости от outcome:
      OK                     → order, user_after_debit
      INSUFFICIENT_BALANCE   → deficit_kopecks, need_kopecks, have_kopecks
      OUT_OF_STOCK           → (нет дополнительных полей)
      SERVICE_DISABLED       → (нет дополнительных полей)
      SERVICE_NOT_FOUND      → (нет дополнительных полей)
      REQUIRES_FIELDS        → required_fields (схема из NS)
      USER_BLOCKED           → (нет дополнительных полей)
    """
    outcome: CheckoutOutcome
    order: ShopOrder | None = None
    user_after_debit: ShopUser | None = None
    # Для INSUFFICIENT_BALANCE
    need_kopecks: int = 0
    have_kopecks: int = 0
    deficit_kopecks: int = 0
    # Для REQUIRES_FIELDS
    required_fields: list[dict[str, Any]] | None = None


def _service_requires_fields(svc: ShopCatalogCache) -> tuple[bool, list[dict[str, Any]] | None]:
    """
    Проверяет, нужно ли запрашивать у юзера дополнительные поля
    (email/username) для NS-выдачи.

    fields_json в shop_catalog_cache — это JSON-список dict'ов схемы
    полей. Пусто или None — поля не нужны (карта приходит готовыми pins).

    Возвращает (требуется_ли, схема_полей_или_None).
    """
    raw = getattr(svc, "fields_json", None)
    if not raw:
        return False, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False, None
    if not isinstance(parsed, list) or len(parsed) == 0:
        return False, None
    # Если в схеме есть хотя бы одно required-поле — требуем заполнить.
    # NS возвращает поля с required: True/False; на момент Sprint 5
    # мы не реализуем UI ввода — поэтому отказываем с REQUIRES_FIELDS.
    has_required = any(
        isinstance(f, dict) and f.get("required", True) for f in parsed
    )
    if has_required:
        return True, parsed
    return False, None


async def attempt_checkout_via_balance(
    session: AsyncSession,
    *,
    user_id: int,
    ns_service_id: int,
) -> CheckoutResult:
    """
    Полный atomic checkout для оплаты из внутреннего баланса.

    Последовательность:
      1. Проверка пользователя (не blocked);
      2. Проверка услуги (существует, enabled, in_stock > 0);
      3. Проверка fields_json (если требует — REQUIRES_FIELDS);
      4. Проверка баланса (≥ цене);
      5. ★ atomic: дебит balance + create_shop_order + mark_order_paid.

    Возвращает CheckoutResult.outcome=OK с заполненными order и user, либо
    одно из failure-состояний с диагностической информацией.

    Транзакция: вся работа в текущей сессии. Вызывающий должен сделать
    session.commit() при OK и session.rollback()/повторное использование
    при ошибке.
    """
    # 1. User
    user = (await session.execute(
        select(ShopUser).where(ShopUser.id == user_id)
    )).scalar_one_or_none()
    if user is None:
        # Пользователь исчез между показом карточки и checkout'ом —
        # крайне маловероятно, но возвращаем понятный outcome.
        return CheckoutResult(outcome=CheckoutOutcome.SERVICE_NOT_FOUND)
    if user.blocked:
        return CheckoutResult(outcome=CheckoutOutcome.USER_BLOCKED)

    # 2. Service
    svc = await get_catalog_service(session, ns_service_id=ns_service_id)
    if svc is None:
        return CheckoutResult(outcome=CheckoutOutcome.SERVICE_NOT_FOUND)
    # enabled уже отфильтрован в get_catalog_service (где enabled=True),
    # но добавим явную проверку для надёжности на случай изменения семантики
    if not svc.enabled:
        return CheckoutResult(outcome=CheckoutOutcome.SERVICE_DISABLED)
    if (svc.in_stock or 0) <= 0:
        return CheckoutResult(outcome=CheckoutOutcome.OUT_OF_STOCK)

    # 3. Fields requirement (Sprint 5: ещё не реализован UI ввода)
    requires_fields, schema = _service_requires_fields(svc)
    if requires_fields:
        return CheckoutResult(
            outcome=CheckoutOutcome.REQUIRES_FIELDS,
            required_fields=schema,
        )

    price = int(svc.rub_price_kopecks or 0)
    if price <= 0:
        # На случай пустой цены — лучше отказать чем создать «бесплатный» заказ
        logger.warning(
            f"checkout: service {ns_service_id} has zero price, blocking"
        )
        return CheckoutResult(outcome=CheckoutOutcome.SERVICE_DISABLED)

    # 4. Balance check
    if user.balance_kopecks < price:
        return CheckoutResult(
            outcome=CheckoutOutcome.INSUFFICIENT_BALANCE,
            need_kopecks=price,
            have_kopecks=user.balance_kopecks,
            deficit_kopecks=price - user.balance_kopecks,
        )

    # 5. Atomic: create order → debit balance → mark paid
    # Если что-то упадёт между шагами — вызывающий должен rollback'нуть
    # session. Все три операции выполняются в одной транзакции.
    order = await create_shop_order(
        session,
        user_id=user.id,
        ns_service_id=svc.ns_service_id,
        ns_service_name=svc.service_name,
        fields_json=svc.fields_json or "[]",
        quantity=1,
        total_rub_kopecks=price,
        ns_price_usd=svc.ns_price_usd,
        payment_method="balance_only",
    )
    user_after = await apply_balance_change(
        session,
        user_id=user.id,
        change_kopecks=-price,
        reason=LEDGER_REASON_ORDER_PAYMENT,
        related_order_id=order.id,
        note=f"Checkout #{order.id}: {svc.service_name[:80]}",
    )
    order = await mark_order_paid(
        session,
        order_id=order.id,
        balance_used_kopecks=price,
        external_paid_kopecks=0,
    )
    logger.info(
        f"shop checkout: user={user.id} order={order.id} paid {price}коп "
        f"({svc.service_name[:60]})"
    )
    return CheckoutResult(
        outcome=CheckoutOutcome.OK,
        order=order,
        user_after_debit=user_after,
    )
