from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Currency
from src.db.models import Base, Order
from src.db.repo import (
    list_reconcilable_orders,
    reserved_quantities_by_service,
)
from src.mapping.rules import PricingResult
from src.ns.models import Service
from src.sync.stock_sync import _risk_skip_reason, _service_with_reserved_stock


@pytest.fixture()
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def test_reserved_stock_never_goes_negative():
    service = Service(
        service_id=10,
        service_name="Apple 5 USD",
        price=4.5,
        currency="USD",
        in_stock=2,
    )

    adjusted = _service_with_reserved_stock(service, reserved=5)

    assert adjusted.in_stock == 0
    assert service.in_stock == 2


def test_guardrail_blocks_low_margin():
    settings = SimpleNamespace(
        sync_min_margin_percent=2.0,
        sync_max_price_change_percent=50.0,
    )
    target = PricingResult(
        ns_price_usd=10.0,
        fx_rate=100.0,
        markup_percent=1.0,
        price_target=1010.0,
        stock=1,
        currency=Currency.RUB,
    )

    reason = _risk_skip_reason(target=target, current_price=1010, settings=settings)

    assert reason is not None
    assert "маржа ниже минимума" in reason


def test_guardrail_blocks_large_price_jump():
    settings = SimpleNamespace(
        sync_min_margin_percent=0.0,
        sync_max_price_change_percent=20.0,
    )
    target = PricingResult(
        ns_price_usd=10.0,
        fx_rate=100.0,
        markup_percent=100.0,
        price_target=2000.0,
        stock=1,
        currency=Currency.RUB,
    )

    reason = _risk_skip_reason(target=target, current_price=1000, settings=settings)

    assert reason is not None
    assert "слишком большое изменение цены" in reason


@pytest.mark.asyncio
async def test_reserved_quantities_counts_only_active_orders(db_factory):
    async with db_factory() as session:
        session.add_all([
            Order(
                funpay_order_id="active-1",
                funpay_lot_id=1,
                ns_service_id=10,
                quantity=2,
                status="ns_created",
            ),
            Order(
                funpay_order_id="active-2",
                funpay_lot_id=2,
                ns_service_id=10,
                quantity=1,
                status="pins_ready",
            ),
            Order(
                funpay_order_id="done",
                funpay_lot_id=3,
                ns_service_id=10,
                quantity=99,
                status="delivered",
            ),
        ])
        await session.commit()

        reserved = await reserved_quantities_by_service(session)

    assert reserved == {10: 3}


@pytest.mark.asyncio
async def test_list_reconcilable_orders_returns_only_stale_intermediate(db_factory):
    now = datetime.utcnow()
    async with db_factory() as session:
        session.add_all([
            Order(
                funpay_order_id="stale",
                funpay_lot_id=1,
                ns_service_id=10,
                status="ns_paid",
                updated_at=now - timedelta(minutes=5),
            ),
            Order(
                funpay_order_id="fresh",
                funpay_lot_id=2,
                ns_service_id=10,
                status="pins_ready",
                updated_at=now,
            ),
            Order(
                funpay_order_id="failed",
                funpay_lot_id=3,
                ns_service_id=10,
                status="failed",
                updated_at=now - timedelta(minutes=5),
            ),
        ])
        await session.commit()

        rows = await list_reconcilable_orders(
            session, stale_after_seconds=60, limit=10
        )

    assert [row.funpay_order_id for row in rows] == ["stale"]
