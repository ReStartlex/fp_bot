"""
Sync `pending_confirmation`: вычистить из БД «фантомные» неподтверждённые заказы.

Боевая проблема. FunPay-саппорт подтверждает заказы по запросу продавца
тихо — НЕ отправляет в чат покупателя системное сообщение
«Администратор X подтвердил...». ChatHandler ничего не ловит, в БД
заказ навсегда остаётся `delivered, confirmed_at=NULL`. За пару месяцев
накопилось ~200 таких заказов, а реально в статусе «Оплачен» на
FunPay сейчас лишь ~20.

Решение: периодически (или по кнопке) сравниваем БД-список
delivered+null с реальным списком «Оплачен» с funpay.com. Всё, чего
нет в списке «Оплачен», считаем тихо подтверждённым саппортом.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Order
from src.db.repo import CONFIRMED_BY_ADMIN, CONFIRMED_BY_BUYER
from src.orders.sync_paid import sync_pending_confirmation


@pytest_asyncio.fixture()
async def session_factory_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_delivered(
    factory,
    funpay_order_id: str,
    *,
    confirmed_at: datetime | None = None,
    confirmed_by: str | None = None,
    status: str = "delivered",
) -> None:
    async with factory() as session:
        session.add(
            Order(
                funpay_order_id=funpay_order_id,
                funpay_lot_id=1,
                ns_service_id=42,
                buyer_username="buyer",
                quantity=1,
                funpay_price_rub=100.0,
                status=status,
                confirmed_at=confirmed_at,
                confirmed_by=confirmed_by,
            )
        )
        await session.commit()


def _make_funpay_client(paid_ids: list[str]) -> MagicMock:
    fp = MagicMock()
    fp.get_paid_sales_snapshot = AsyncMock(return_value=paid_ids)
    return fp


# ─────────────── Тесты ───────────────


@pytest.mark.asyncio
async def test_marks_orders_not_in_paid_list_as_confirmed(
    session_factory_fixture,
):
    """
    Главный кейс. 3 заказа в БД (delivered+null), на FunPay в paid
    только 1 → 2 остальных закрыл саппорт тихо → помечаем confirmed_at.
    """
    await _seed_delivered(session_factory_fixture, "ORDER1")
    await _seed_delivered(session_factory_fixture, "ORDER2")
    await _seed_delivered(session_factory_fixture, "ORDER3")
    fp = _make_funpay_client(paid_ids=["ORDER1"])

    stats = await sync_pending_confirmation(
        funpay_client=fp,
        session_factory=lambda: session_factory_fixture,
    )

    assert stats["paid_on_funpay"] == 1
    assert stats["delivered_unconfirmed_in_db"] == 3
    assert stats["marked_confirmed"] == 2

    async with session_factory_fixture() as session:
        from sqlalchemy import select

        result = await session.execute(select(Order).order_by(Order.funpay_order_id))
        orders = list(result.scalars().all())

    by_id = {o.funpay_order_id: o for o in orders}
    assert by_id["ORDER1"].confirmed_at is None
    assert by_id["ORDER2"].confirmed_at is not None
    assert by_id["ORDER2"].confirmed_by == CONFIRMED_BY_ADMIN
    assert by_id["ORDER3"].confirmed_at is not None
    assert by_id["ORDER3"].confirmed_by == CONFIRMED_BY_ADMIN


@pytest.mark.asyncio
async def test_does_not_touch_already_confirmed_orders(
    session_factory_fixture,
):
    """
    Анти-регрессия: ордера, у которых confirmed_at УЖЕ установлен
    (например, через mark_order_confirmed из chat-handler), не должны
    перетираться этим sync'ом — даже если их нет в paid-списке.
    """
    earlier = datetime.utcnow() - timedelta(days=5)
    await _seed_delivered(
        session_factory_fixture,
        "BUYER_CONFIRMED",
        confirmed_at=earlier,
        confirmed_by=CONFIRMED_BY_BUYER,
    )
    fp = _make_funpay_client(paid_ids=[])

    stats = await sync_pending_confirmation(
        funpay_client=fp,
        session_factory=lambda: session_factory_fixture,
    )

    assert stats["marked_confirmed"] == 0

    async with session_factory_fixture() as session:
        from sqlalchemy import select

        order = (
            await session.execute(
                select(Order).where(Order.funpay_order_id == "BUYER_CONFIRMED")
            )
        ).scalar_one()
        # Сохранили исходное confirmed_at (с точностью до секунд) и by
        assert abs((order.confirmed_at - earlier).total_seconds()) < 1
        assert order.confirmed_by == CONFIRMED_BY_BUYER


@pytest.mark.asyncio
async def test_empty_paid_list_marks_all_delivered_orders(
    session_factory_fixture,
):
    """
    Если на FunPay вообще нет «Оплачен» — значит саппорт всё подтвердил.
    Все delivered+null в БД должны быть помечены.
    """
    await _seed_delivered(session_factory_fixture, "A1")
    await _seed_delivered(session_factory_fixture, "A2")
    fp = _make_funpay_client(paid_ids=[])

    stats = await sync_pending_confirmation(
        funpay_client=fp,
        session_factory=lambda: session_factory_fixture,
    )

    assert stats["marked_confirmed"] == 2


@pytest.mark.asyncio
async def test_empty_db_returns_zero_stats(session_factory_fixture):
    """Если в БД нет delivered+null заказов — sync ничего не делает."""
    fp = _make_funpay_client(paid_ids=["A1", "B2"])

    stats = await sync_pending_confirmation(
        funpay_client=fp,
        session_factory=lambda: session_factory_fixture,
    )

    assert stats == {
        "paid_on_funpay": 2,
        "delivered_unconfirmed_in_db": 0,
        "marked_confirmed": 0,
    }


@pytest.mark.asyncio
async def test_paid_id_unknown_to_db_is_ignored(session_factory_fixture):
    """
    На FunPay есть paid-заказы, которых нет в нашей БД (например,
    ручная выкладка лотов — мы их не выдавали через бота). Это норма:
    sync должен их игнорировать, не падать.
    """
    await _seed_delivered(session_factory_fixture, "OUR1")
    fp = _make_funpay_client(paid_ids=["NOT_OURS_HUMAN_SOLD"])

    stats = await sync_pending_confirmation(
        funpay_client=fp,
        session_factory=lambda: session_factory_fixture,
    )

    # OUR1 нет среди paid → помечаем закрытым
    assert stats["marked_confirmed"] == 1
    assert stats["paid_on_funpay"] == 1
    assert stats["delivered_unconfirmed_in_db"] == 1


@pytest.mark.asyncio
async def test_does_not_touch_orders_in_other_statuses(
    session_factory_fixture,
):
    """
    Анти-регрессия: заказ в статусе received/ns_paid/manual_hold не
    должен трогаться (он ещё не выдан покупателю, а sync — про уже
    выданные delivered).
    """
    await _seed_delivered(session_factory_fixture, "RECEIVED1", status="received")
    await _seed_delivered(session_factory_fixture, "PAID1", status="ns_paid")
    await _seed_delivered(session_factory_fixture, "HOLD1", status="manual_hold")
    await _seed_delivered(session_factory_fixture, "DELIVERED1", status="delivered")
    fp = _make_funpay_client(paid_ids=[])

    stats = await sync_pending_confirmation(
        funpay_client=fp,
        session_factory=lambda: session_factory_fixture,
    )

    assert stats["delivered_unconfirmed_in_db"] == 1  # только DELIVERED1
    assert stats["marked_confirmed"] == 1

    async with session_factory_fixture() as session:
        from sqlalchemy import select

        result = await session.execute(select(Order))
        by_id = {o.funpay_order_id: o for o in result.scalars().all()}
    assert by_id["RECEIVED1"].confirmed_at is None
    assert by_id["PAID1"].confirmed_at is None
    assert by_id["HOLD1"].confirmed_at is None
    assert by_id["DELIVERED1"].confirmed_at is not None
