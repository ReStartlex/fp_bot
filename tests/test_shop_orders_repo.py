"""
Sprint 5 — repo функции для ShopOrder + cashback.

Здесь покрываем критически важные инварианты:
  * Создание / переходы статусов
  * Идемпотентность cashback (нельзя начислить дважды)
  * Идемпотентность refund (нельзя вернуть дважды)
  * Edge cases: no inviter, inviter==buyer, низкий cashback_percent
  * list_user_orders / list_orders_awaiting_delivery — pagination
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, ShopBalanceLedger, ShopUser
from src.shop.repo import (
    LEDGER_REASON_ORDER_PAYMENT,
    LEDGER_REASON_REFERRAL_CASHBACK,
    LEDGER_REASON_REFUND,
    SHOP_ORDER_STATUS_DELIVERED,
    SHOP_ORDER_STATUS_DELIVERING,
    SHOP_ORDER_STATUS_DRAFT,
    SHOP_ORDER_STATUS_FAILED,
    SHOP_ORDER_STATUS_PAID,
    SHOP_ORDER_STATUS_REFUNDED,
    apply_balance_change,
    attach_referral,
    create_shop_order,
    credit_referral_cashback,
    get_or_create_user,
    get_shop_order,
    list_orders_awaiting_delivery,
    list_user_orders,
    mark_order_delivered,
    mark_order_delivering,
    mark_order_failed,
    mark_order_paid,
    refund_failed_order,
)


@pytest.fixture()
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _make_user(s, *, tg_id: int) -> ShopUser:
    user, _ = await get_or_create_user(
        s, telegram_user_id=tg_id,
    )
    return user


async def _make_user_with_inviter(s, *, buyer_tg: int, inviter_tg: int):
    """Создаёт пару (inviter, buyer), где buyer привязан к inviter."""
    inviter = await _make_user(s, tg_id=inviter_tg)
    buyer = await _make_user(s, tg_id=buyer_tg)
    await attach_referral(
        s, referrer_user_id=inviter.id, referred_user_id=buyer.id,
    )
    await s.commit()
    return inviter, buyer


# ───────────── create_shop_order + переходы статусов ─────────────


async def test_create_shop_order_starts_in_draft(db_factory):
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        await s.commit()
    async with db_factory() as s:
        order = await create_shop_order(
            s, user_id=user.id,
            ns_service_id=1, ns_service_name="Apple US $5",
            total_rub_kopecks=40000,
        )
        await s.commit()
    assert order.status == SHOP_ORDER_STATUS_DRAFT
    assert order.id is not None
    assert order.balance_used_kopecks == 0


async def test_mark_order_paid_records_balance_used(db_factory):
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        order = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await s.commit()
        order = await mark_order_paid(
            s, order_id=order.id,
            balance_used_kopecks=40000,
        )
        await s.commit()
    assert order.status == SHOP_ORDER_STATUS_PAID
    assert order.balance_used_kopecks == 40000
    assert order.paid_at is not None


async def test_mark_order_delivering_writes_custom_id(db_factory):
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        order = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await s.commit()
        await mark_order_paid(
            s, order_id=order.id, balance_used_kopecks=40000,
        )
        order = await mark_order_delivering(
            s, order_id=order.id, ns_custom_id="shop-1-uuid",
            ns_order_id="ns-12345",
        )
        await s.commit()
    assert order.status == SHOP_ORDER_STATUS_DELIVERING
    assert order.ns_custom_id == "shop-1-uuid"
    assert order.ns_order_id == "ns-12345"


async def test_mark_order_delivered_saves_pins(db_factory):
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        order = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await s.commit()
        await mark_order_paid(s, order_id=order.id, balance_used_kopecks=40000)
        await mark_order_delivering(s, order_id=order.id, ns_custom_id="x")
        order = await mark_order_delivered(
            s, order_id=order.id, pins_json='[{"code":"ABC123"}]',
        )
        await s.commit()
    assert order.status == SHOP_ORDER_STATUS_DELIVERED
    assert "ABC123" in (order.pins_json or "")
    assert order.delivered_at is not None


async def test_mark_order_failed_records_error(db_factory):
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        order = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await s.commit()
        order = await mark_order_failed(
            s, order_id=order.id, error="NS error: insufficient",
        )
        await s.commit()
    assert order.status == SHOP_ORDER_STATUS_FAILED
    assert "insufficient" in order.error


# ───────────── refund_failed_order: идемпотентность ─────────────


async def test_refund_returns_balance_to_user(db_factory):
    """После failed-заказа refund вернёт balance_used обратно."""
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        await s.commit()
    # Симулируем checkout: дебитим, создаём заказ
    async with db_factory() as s:
        await apply_balance_change(
            s, user_id=user.id, change_kopecks=100000,
            reason="manual_topup",
        )
        order = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await apply_balance_change(
            s, user_id=user.id, change_kopecks=-40000,
            reason=LEDGER_REASON_ORDER_PAYMENT,
            related_order_id=order.id,
        )
        await mark_order_paid(
            s, order_id=order.id, balance_used_kopecks=40000,
        )
        await mark_order_failed(s, order_id=order.id, error="boom")
        await s.commit()
    # До refund — balance 60000
    async with db_factory() as s:
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == user.id)
        )).scalar_one()
        assert u.balance_kopecks == 60000

    async with db_factory() as s:
        await refund_failed_order(s, order_id=order.id)
        await s.commit()

    async with db_factory() as s:
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == user.id)
        )).scalar_one()
        order_refunded = await get_shop_order(s, order.id)
    assert u.balance_kopecks == 100000, "Balance восстановился"
    assert order_refunded.status == SHOP_ORDER_STATUS_REFUNDED


async def test_refund_is_idempotent(db_factory):
    """Повторный refund не должен дублировать возврат денег."""
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        await s.commit()
    async with db_factory() as s:
        await apply_balance_change(
            s, user_id=user.id, change_kopecks=100000, reason="manual_topup",
        )
        order = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await apply_balance_change(
            s, user_id=user.id, change_kopecks=-40000,
            reason=LEDGER_REASON_ORDER_PAYMENT,
            related_order_id=order.id,
        )
        await mark_order_paid(s, order_id=order.id, balance_used_kopecks=40000)
        await mark_order_failed(s, order_id=order.id, error="boom")
        await s.commit()

    async with db_factory() as s:
        await refund_failed_order(s, order_id=order.id)
        await s.commit()
    async with db_factory() as s:
        await refund_failed_order(s, order_id=order.id)  # повтор
        await s.commit()

    async with db_factory() as s:
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == user.id)
        )).scalar_one()
        # Проверяем что в ledger ровно ОДНА refund запись
        refunds = (await s.execute(
            select(ShopBalanceLedger).where(
                ShopBalanceLedger.user_id == user.id,
                ShopBalanceLedger.reason == LEDGER_REASON_REFUND,
            )
        )).scalars().all()
    assert u.balance_kopecks == 100000, "Balance вернулся ровно один раз"
    assert len(refunds) == 1


async def test_refund_skips_if_balance_used_is_zero(db_factory):
    """Заказ оплачен полностью извне (balance_used=0) — refund внешний, не наш."""
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        await s.commit()
    async with db_factory() as s:
        order = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await mark_order_paid(
            s, order_id=order.id,
            balance_used_kopecks=0,
            external_paid_kopecks=40000,
        )
        await mark_order_failed(s, order_id=order.id, error="boom")
        await s.commit()

    async with db_factory() as s:
        await refund_failed_order(s, order_id=order.id)
        await s.commit()

    async with db_factory() as s:
        # Заказ помечен REFUNDED, но никаких ledger-записей не создано
        order2 = await get_shop_order(s, order.id)
        refunds = (await s.execute(
            select(ShopBalanceLedger).where(
                ShopBalanceLedger.user_id == user.id,
                ShopBalanceLedger.reason == LEDGER_REASON_REFUND,
            )
        )).scalars().all()
    assert order2.status == SHOP_ORDER_STATUS_REFUNDED
    assert refunds == []


# ───────────── credit_referral_cashback: основное + идемпотентность ──


async def test_cashback_credits_inviter_one_percent(db_factory):
    """Базовый случай: 1% от 400₽ = 4₽."""
    async with db_factory() as s:
        inviter, buyer = await _make_user_with_inviter(
            s, buyer_tg=200, inviter_tg=100,
        )
    async with db_factory() as s:
        order = await create_shop_order(
            s, user_id=buyer.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,  # 400₽
        )
        await mark_order_paid(s, order_id=order.id, balance_used_kopecks=40000)
        await mark_order_delivering(s, order_id=order.id, ns_custom_id="x")
        await mark_order_delivered(s, order_id=order.id, pins_json="[]")
        await s.commit()

    async with db_factory() as s:
        credited = await credit_referral_cashback(
            s, order_id=order.id, cashback_percent=1.0,
        )
        await s.commit()
    assert credited == 400  # 1% от 40000 копеек = 400 копеек = 4₽

    async with db_factory() as s:
        inviter_after = (await s.execute(
            select(ShopUser).where(ShopUser.id == inviter.id)
        )).scalar_one()
    assert inviter_after.balance_kopecks == 400


async def test_cashback_is_idempotent(db_factory):
    """Повторный credit_referral_cashback не должен начислить вторую раз."""
    async with db_factory() as s:
        inviter, buyer = await _make_user_with_inviter(
            s, buyer_tg=200, inviter_tg=100,
        )
    async with db_factory() as s:
        order = await create_shop_order(
            s, user_id=buyer.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await mark_order_paid(s, order_id=order.id, balance_used_kopecks=40000)
        await mark_order_delivering(s, order_id=order.id, ns_custom_id="x")
        await mark_order_delivered(s, order_id=order.id, pins_json="[]")
        await s.commit()

    async with db_factory() as s:
        c1 = await credit_referral_cashback(
            s, order_id=order.id, cashback_percent=1.0,
        )
        await s.commit()
    async with db_factory() as s:
        c2 = await credit_referral_cashback(
            s, order_id=order.id, cashback_percent=1.0,
        )
        await s.commit()
    assert c1 == 400
    assert c2 == 0, "Повторное начисление = 0"

    async with db_factory() as s:
        inviter_after = (await s.execute(
            select(ShopUser).where(ShopUser.id == inviter.id)
        )).scalar_one()
        cashbacks = (await s.execute(
            select(ShopBalanceLedger).where(
                ShopBalanceLedger.user_id == inviter.id,
                ShopBalanceLedger.reason == LEDGER_REASON_REFERRAL_CASHBACK,
            )
        )).scalars().all()
    assert inviter_after.balance_kopecks == 400, "Начислено ровно один раз"
    assert len(cashbacks) == 1


async def test_cashback_skipped_if_no_inviter(db_factory):
    """Покупатель без referred_by_user_id — cashback = 0."""
    async with db_factory() as s:
        buyer = await _make_user(s, tg_id=200)
        await s.commit()
    async with db_factory() as s:
        order = await create_shop_order(
            s, user_id=buyer.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await mark_order_paid(s, order_id=order.id, balance_used_kopecks=40000)
        await mark_order_delivering(s, order_id=order.id, ns_custom_id="x")
        await mark_order_delivered(s, order_id=order.id, pins_json="[]")
        await s.commit()
    async with db_factory() as s:
        credited = await credit_referral_cashback(
            s, order_id=order.id, cashback_percent=1.0,
        )
    assert credited == 0


async def test_cashback_skipped_if_order_not_delivered(db_factory):
    """Заказ ещё в delivering — cashback нельзя начислять (риск возврата)."""
    async with db_factory() as s:
        inviter, buyer = await _make_user_with_inviter(
            s, buyer_tg=200, inviter_tg=100,
        )
    async with db_factory() as s:
        order = await create_shop_order(
            s, user_id=buyer.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await mark_order_paid(s, order_id=order.id, balance_used_kopecks=40000)
        await mark_order_delivering(s, order_id=order.id, ns_custom_id="x")
        await s.commit()
    async with db_factory() as s:
        credited = await credit_referral_cashback(
            s, order_id=order.id, cashback_percent=1.0,
        )
    assert credited == 0, "До delivered — cashback не выдаём"


async def test_cashback_zero_percent_is_noop(db_factory):
    """cashback_percent=0 — даже не пробуем считать (защита от мусора)."""
    async with db_factory() as s:
        inviter, buyer = await _make_user_with_inviter(
            s, buyer_tg=200, inviter_tg=100,
        )
    async with db_factory() as s:
        order = await create_shop_order(
            s, user_id=buyer.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=40000,
        )
        await mark_order_paid(s, order_id=order.id, balance_used_kopecks=40000)
        await mark_order_delivering(s, order_id=order.id, ns_custom_id="x")
        await mark_order_delivered(s, order_id=order.id, pins_json="[]")
        await s.commit()
    async with db_factory() as s:
        credited = await credit_referral_cashback(
            s, order_id=order.id, cashback_percent=0,
        )
    assert credited == 0


async def test_cashback_tiny_order_no_kopecks(db_factory):
    """Заказ на 50 копеек × 1% = 0.5 коп → floor = 0, ничего не начисляем."""
    async with db_factory() as s:
        inviter, buyer = await _make_user_with_inviter(
            s, buyer_tg=200, inviter_tg=100,
        )
    async with db_factory() as s:
        order = await create_shop_order(
            s, user_id=buyer.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=50,
        )
        await mark_order_paid(s, order_id=order.id, balance_used_kopecks=50)
        await mark_order_delivering(s, order_id=order.id, ns_custom_id="x")
        await mark_order_delivered(s, order_id=order.id, pins_json="[]")
        await s.commit()
    async with db_factory() as s:
        credited = await credit_referral_cashback(
            s, order_id=order.id, cashback_percent=1.0,
        )
    assert credited == 0


# ───────────── list_user_orders / list_orders_awaiting_delivery ──


async def test_list_user_orders_newest_first(db_factory):
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        await s.commit()
    async with db_factory() as s:
        for i in range(5):
            await create_shop_order(
                s, user_id=user.id, ns_service_id=i,
                ns_service_name=f"X{i}",
                total_rub_kopecks=1000 * (i + 1),
            )
        await s.commit()
    async with db_factory() as s:
        orders, total = await list_user_orders(
            s, user_id=user.id, limit=10,
        )
    assert total == 5
    assert len(orders) == 5
    # Newest first → service_id убывает (4, 3, 2, 1, 0)
    assert [o.ns_service_id for o in orders] == [4, 3, 2, 1, 0]


async def test_list_user_orders_pagination(db_factory):
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        await s.commit()
    async with db_factory() as s:
        for i in range(7):
            await create_shop_order(
                s, user_id=user.id, ns_service_id=i,
                ns_service_name=f"X{i}", total_rub_kopecks=1000,
            )
        await s.commit()
    async with db_factory() as s:
        page1, total = await list_user_orders(
            s, user_id=user.id, limit=3, offset=0,
        )
        page2, _ = await list_user_orders(
            s, user_id=user.id, limit=3, offset=3,
        )
    assert total == 7
    assert len(page1) == 3
    assert len(page2) == 3
    # Между страницами нет пересечений
    p1_ids = {o.id for o in page1}
    p2_ids = {o.id for o in page2}
    assert p1_ids.isdisjoint(p2_ids)


async def test_list_awaiting_delivery_returns_paid_and_delivering(db_factory):
    """В worker'е берём PAID и DELIVERING заказы — оба активны."""
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        await s.commit()
    async with db_factory() as s:
        # 1 draft (НЕ берём)
        o1 = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="X", total_rub_kopecks=1000,
        )
        # 1 paid
        o2 = await create_shop_order(
            s, user_id=user.id, ns_service_id=2,
            ns_service_name="X", total_rub_kopecks=1000,
        )
        await mark_order_paid(s, order_id=o2.id, balance_used_kopecks=1000)
        # 1 delivering
        o3 = await create_shop_order(
            s, user_id=user.id, ns_service_id=3,
            ns_service_name="X", total_rub_kopecks=1000,
        )
        await mark_order_paid(s, order_id=o3.id, balance_used_kopecks=1000)
        await mark_order_delivering(s, order_id=o3.id, ns_custom_id="z")
        # 1 delivered (НЕ берём)
        o4 = await create_shop_order(
            s, user_id=user.id, ns_service_id=4,
            ns_service_name="X", total_rub_kopecks=1000,
        )
        await mark_order_paid(s, order_id=o4.id, balance_used_kopecks=1000)
        await mark_order_delivering(s, order_id=o4.id, ns_custom_id="zz")
        await mark_order_delivered(s, order_id=o4.id, pins_json="[]")
        await s.commit()

    async with db_factory() as s:
        awaiting = await list_orders_awaiting_delivery(s, limit=10)
    sids = {o.ns_service_id for o in awaiting}
    assert sids == {2, 3}, (
        f"Должны быть только paid+delivering: {sids}"
    )


async def test_list_awaiting_delivery_respects_limit(db_factory):
    """limit ограничивает выборку."""
    async with db_factory() as s:
        user = await _make_user(s, tg_id=100)
        await s.commit()
    async with db_factory() as s:
        for i in range(7):
            o = await create_shop_order(
                s, user_id=user.id, ns_service_id=i,
                ns_service_name="X", total_rub_kopecks=1000,
            )
            await mark_order_paid(s, order_id=o.id, balance_used_kopecks=1000)
        await s.commit()
    async with db_factory() as s:
        rows = await list_orders_awaiting_delivery(s, limit=3)
    assert len(rows) == 3
