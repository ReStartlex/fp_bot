"""
Тесты подтверждения успешного выполнения заказа.

Покрывают:
- mark_order_confirmed: BUYER vs ADMIN confirmation,
  идемпотентность (повторный вызов не перетирает),
  заказ не найден → None;
- list_pending_confirmation: фильтр по status=delivered,
  по cutoff, по NULL confirmed_at, сортировка;
- невалидный confirmed_by → ValueError.

Используем in-memory SQLite через тот же паттерн что в
test_order_processor.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Order
from src.db.repo import (
    CONFIRMED_BY_ADMIN,
    CONFIRMED_BY_BUYER,
    list_pending_confirmation,
    mark_order_confirmed,
)


@pytest.fixture()
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _create_order(
    factory,
    *,
    funpay_order_id: str,
    status: str = "delivered",
    updated_at: datetime | None = None,
    confirmed_at: datetime | None = None,
    confirmed_by: str | None = None,
) -> Order:
    async with factory() as session:
        order = Order(
            funpay_order_id=funpay_order_id,
            funpay_lot_id=1,
            ns_service_id=42,
            buyer_username="Macan1467",
            quantity=1,
            funpay_price_rub=100.0,
            status=status,
            confirmed_at=confirmed_at,
            confirmed_by=confirmed_by,
        )
        session.add(order)
        await session.flush()
        if updated_at is not None:
            order.updated_at = updated_at
            await session.flush()
        await session.commit()
        return order


# ─────────────────────── mark_order_confirmed ───────────────────────


@pytest.mark.asyncio
async def test_mark_order_confirmed_by_buyer(session_factory):
    await _create_order(session_factory, funpay_order_id="C4KPFX6M")

    async with session_factory() as session:
        order = await mark_order_confirmed(
            session,
            funpay_order_id="C4KPFX6M",
            confirmed_by=CONFIRMED_BY_BUYER,
        )
        await session.commit()

    assert order is not None
    assert order.confirmed_at is not None
    assert order.confirmed_by == "buyer"


@pytest.mark.asyncio
async def test_mark_order_confirmed_by_admin(session_factory):
    """Основной кейс новой фичи."""
    await _create_order(session_factory, funpay_order_id="UGW9A7CQ")

    async with session_factory() as session:
        order = await mark_order_confirmed(
            session,
            funpay_order_id="UGW9A7CQ",
            confirmed_by=CONFIRMED_BY_ADMIN,
        )
        await session.commit()

    assert order is not None
    assert order.confirmed_at is not None
    assert order.confirmed_by == "admin"


@pytest.mark.asyncio
async def test_mark_order_confirmed_returns_none_when_not_found(session_factory):
    """Заказа нет в БД (например, выдан до запуска бота) → None."""
    async with session_factory() as session:
        order = await mark_order_confirmed(
            session,
            funpay_order_id="UNKNOWN12",
            confirmed_by=CONFIRMED_BY_ADMIN,
        )
        await session.commit()
    assert order is None


@pytest.mark.asyncio
async def test_mark_order_confirmed_is_idempotent(session_factory):
    """
    Повторный вызов с тем же order_id НЕ перетирает первое подтверждение.
    Важно потому что:
    1) FunPay может прислать дубль системного сообщения;
    2) Сценарий: buyer подтвердил → потом саппорт тоже жмёт «подтвердить»
       (если статус сбоит). Сохраняем «первое» подтверждение для
       честной аналитики.
    """
    await _create_order(session_factory, funpay_order_id="C4KPFX6M")

    async with session_factory() as session:
        order1 = await mark_order_confirmed(
            session,
            funpay_order_id="C4KPFX6M",
            confirmed_by=CONFIRMED_BY_BUYER,
        )
        await session.commit()
        first_confirmed_at = order1.confirmed_at

    async with session_factory() as session:
        order2 = await mark_order_confirmed(
            session,
            funpay_order_id="C4KPFX6M",
            confirmed_by=CONFIRMED_BY_ADMIN,  # пытаемся «перетереть» админом
        )
        await session.commit()

    assert order2 is not None
    assert order2.confirmed_by == "buyer", (
        "Идемпотентность нарушена: второй вызов перетёр buyer на admin"
    )
    assert order2.confirmed_at == first_confirmed_at, (
        "confirmed_at не должен меняться при повторном подтверждении"
    )


@pytest.mark.asyncio
async def test_mark_order_confirmed_rejects_invalid_confirmed_by(session_factory):
    await _create_order(session_factory, funpay_order_id="C4KPFX6M")
    async with session_factory() as session:
        with pytest.raises(ValueError, match="invalid confirmed_by"):
            await mark_order_confirmed(
                session,
                funpay_order_id="C4KPFX6M",
                confirmed_by="moderator",  # нет такого значения
            )


# ──────────────────── list_pending_confirmation ────────────────────


@pytest.mark.asyncio
async def test_list_pending_confirmation_returns_old_unconfirmed_delivered(
    session_factory,
):
    """Заказ выдан 25ч назад, не подтверждён → должен попасть."""
    long_ago = datetime.utcnow() - timedelta(hours=25)
    await _create_order(
        session_factory,
        funpay_order_id="OLD12345",
        status="delivered",
        updated_at=long_ago,
    )

    async with session_factory() as session:
        rows = await list_pending_confirmation(session, older_than_hours=24)

    assert len(rows) == 1
    assert rows[0].funpay_order_id == "OLD12345"


@pytest.mark.asyncio
async def test_list_pending_confirmation_excludes_already_confirmed(session_factory):
    """Заказ подтверждён → НЕ должен попасть, даже если старый."""
    long_ago = datetime.utcnow() - timedelta(hours=25)
    await _create_order(
        session_factory,
        funpay_order_id="DONE1234",
        status="delivered",
        updated_at=long_ago,
        confirmed_at=datetime.utcnow(),
        confirmed_by="buyer",
    )

    async with session_factory() as session:
        rows = await list_pending_confirmation(session, older_than_hours=24)

    assert rows == []


@pytest.mark.asyncio
async def test_list_pending_confirmation_excludes_recent_orders(session_factory):
    """Заказ выдан 2ч назад → ещё рано (24ч cutoff)."""
    recent = datetime.utcnow() - timedelta(hours=2)
    await _create_order(
        session_factory,
        funpay_order_id="FRESH123",
        status="delivered",
        updated_at=recent,
    )

    async with session_factory() as session:
        rows = await list_pending_confirmation(session, older_than_hours=24)

    assert rows == []


@pytest.mark.asyncio
async def test_list_pending_confirmation_excludes_non_delivered_statuses(
    session_factory,
):
    """Только delivered. failed/refunded/manual_hold не интересны для саппорта."""
    long_ago = datetime.utcnow() - timedelta(hours=25)
    for status in ("failed", "refunded", "manual_hold", "ns_paid", "pins_ready"):
        await _create_order(
            session_factory,
            funpay_order_id=f"{status[:6].upper()}99",
            status=status,
            updated_at=long_ago,
        )

    async with session_factory() as session:
        rows = await list_pending_confirmation(session, older_than_hours=24)

    assert rows == []


@pytest.mark.asyncio
async def test_list_pending_confirmation_sorts_oldest_first(session_factory):
    """Сортировка ASC по updated_at: самые «горящие» первыми."""
    base = datetime.utcnow()
    await _create_order(
        session_factory,
        funpay_order_id="ORDER_B",
        status="delivered",
        updated_at=base - timedelta(hours=30),
    )
    await _create_order(
        session_factory,
        funpay_order_id="ORDER_A",
        status="delivered",
        updated_at=base - timedelta(hours=50),  # самый старый
    )
    await _create_order(
        session_factory,
        funpay_order_id="ORDER_C",
        status="delivered",
        updated_at=base - timedelta(hours=25),
    )

    async with session_factory() as session:
        rows = await list_pending_confirmation(session, older_than_hours=24)

    ids = [r.funpay_order_id for r in rows]
    assert ids == ["ORDER_A", "ORDER_B", "ORDER_C"]


@pytest.mark.asyncio
async def test_list_pending_confirmation_respects_limit(session_factory):
    long_ago = datetime.utcnow() - timedelta(hours=30)
    for i in range(5):
        await _create_order(
            session_factory,
            funpay_order_id=f"ORD{i:05d}",
            status="delivered",
            updated_at=long_ago - timedelta(seconds=i),
        )

    async with session_factory() as session:
        rows = await list_pending_confirmation(
            session, older_than_hours=24, limit=2
        )

    assert len(rows) == 2


@pytest.mark.asyncio
async def test_list_pending_confirmation_custom_cutoff_hours(session_factory):
    """Можно настроить cutoff (e.g. 12ч если хотим раньше беспокоить саппорт)."""
    six_hours_ago = datetime.utcnow() - timedelta(hours=6)
    await _create_order(
        session_factory,
        funpay_order_id="SIXHRS00",
        status="delivered",
        updated_at=six_hours_ago,
    )

    async with session_factory() as session:
        rows_24h = await list_pending_confirmation(session, older_than_hours=24)
        rows_4h = await list_pending_confirmation(session, older_than_hours=4)

    assert rows_24h == []
    assert len(rows_4h) == 1
