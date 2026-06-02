"""
Smoke-тесты shop-моделей: create_all не падает, UNIQUE-constraint'ы
работают, invariant `sum(ledger.change) == user.balance` соблюдён.

Эти тесты — первая защита от типовых ошибок в БД (опечатки в имени
колонки, забытый UniqueConstraint, неправильный default).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import (
    Base,
    ShopBalanceLedger,
    ShopCatalogCache,
    ShopOrder,
    ShopPayment,
    ShopReferral,
    ShopUser,
)


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


async def test_create_all_includes_shop_tables(factory):
    """Все 6 shop-таблиц создаются через Base.metadata.create_all."""
    expected = {
        "shop_users",
        "shop_referrals",
        "shop_orders",
        "shop_payments",
        "shop_balance_ledger",
        "shop_catalog_cache",
    }
    actual = set(Base.metadata.tables.keys())
    assert expected.issubset(actual), f"missing: {expected - actual}"


async def test_shop_user_unique_telegram_user_id(factory):
    """Два юзера с одним telegram_user_id → IntegrityError."""
    async with factory() as s:
        s.add(ShopUser(telegram_user_id=12345, first_name="A"))
        await s.commit()
    async with factory() as s:
        s.add(ShopUser(telegram_user_id=12345, first_name="B"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_shop_user_defaults(factory):
    """balance_kopecks=0, blocked=False по умолчанию."""
    async with factory() as s:
        u = ShopUser(telegram_user_id=1, first_name="x")
        s.add(u)
        await s.commit()
        await s.refresh(u)
        assert u.balance_kopecks == 0
        assert u.blocked is False
        assert u.referred_by_user_id is None


async def test_shop_referral_unique_referred_user_id(factory):
    """Один реферал не может быть привязан к двум inviter'ам."""
    async with factory() as s:
        s.add(ShopReferral(referrer_user_id=1, referred_user_id=2))
        await s.commit()
    async with factory() as s:
        s.add(ShopReferral(referrer_user_id=99, referred_user_id=2))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_shop_payment_idempotency_via_unique(factory):
    """
    UNIQUE(provider, provider_invoice_id) защищает от replay webhook'а:
    повторная вставка того же invoice падает на уровне БД.
    """
    async with factory() as s:
        s.add(ShopPayment(
            order_id=1, provider="cryptobot",
            provider_invoice_id="inv-100",
            amount_kopecks=1000, currency="RUB", status="paid",
        ))
        await s.commit()
    async with factory() as s:
        s.add(ShopPayment(
            order_id=1, provider="cryptobot",
            provider_invoice_id="inv-100",
            amount_kopecks=1000, currency="RUB", status="paid",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_shop_payment_same_invoice_different_provider_ok(factory):
    """Одно и то же id у разных провайдеров — это разные платежи."""
    async with factory() as s:
        s.add(ShopPayment(
            order_id=1, provider="cryptobot",
            provider_invoice_id="100", amount_kopecks=1000, status="paid",
        ))
        s.add(ShopPayment(
            order_id=1, provider="stars",
            provider_invoice_id="100", amount_kopecks=1000, status="paid",
        ))
        await s.commit()


async def test_ledger_sum_equals_user_balance_invariant(factory):
    """
    Инвариант: sum(ledger.change_kopecks где user_id=X)
    == shop_users.balance_kopecks для X. Это базовое требование к коду,
    который меняет баланс. Тест проверяет, что мы как минимум умеем
    держать инвариант в одной транзакции.
    """
    async with factory() as s:
        u = ShopUser(telegram_user_id=1, balance_kopecks=0)
        s.add(u)
        await s.flush()
        uid = u.id

        s.add(ShopBalanceLedger(
            user_id=uid, change_kopecks=500,
            reason="referral_cashback", note="invite alice",
        ))
        u.balance_kopecks = 500

        s.add(ShopBalanceLedger(
            user_id=uid, change_kopecks=-200,
            reason="order_payment", related_order_id=42,
        ))
        u.balance_kopecks = 300

        await s.commit()

    async with factory() as s:
        u = (await s.execute(select(ShopUser).where(ShopUser.id == uid))).scalar_one()
        total = (
            await s.execute(
                select(ShopBalanceLedger).where(ShopBalanceLedger.user_id == uid)
            )
        ).scalars().all()
        assert u.balance_kopecks == sum(e.change_kopecks for e in total)


async def test_shop_order_defaults(factory):
    """draft по умолчанию, balance_used=0, external_paid=0."""
    async with factory() as s:
        order = ShopOrder(
            user_id=1,
            ns_service_id=42,
            ns_service_name="Apple Gift Card $5",
            fields_json="[]",
            quantity=1,
            total_rub_kopecks=50000,
        )
        s.add(order)
        await s.commit()
        await s.refresh(order)
        assert order.status == "draft"
        assert order.balance_used_kopecks == 0
        assert order.external_paid_kopecks == 0
        assert order.paid_at is None
        assert order.delivered_at is None


async def test_shop_catalog_cache_upsert(factory):
    """Каталог индексируется по ns_service_id (PK), повторная вставка
    падает — обновление должно идти через UPDATE / merge."""
    async with factory() as s:
        s.add(ShopCatalogCache(
            ns_service_id=10, category_id=1, category_name="Apple",
            service_name="Apple $5", ns_price_usd=5.0, rub_price_kopecks=50000,
            in_stock=100, enabled=True,
        ))
        await s.commit()
    async with factory() as s:
        with pytest.raises(IntegrityError):
            s.add(ShopCatalogCache(
                ns_service_id=10, service_name="dup",
                ns_price_usd=5.0, rub_price_kopecks=50000,
            ))
            await s.commit()
