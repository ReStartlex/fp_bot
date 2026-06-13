"""
P2-5: суточная статистика (daily_stats).

Проверяем:
  1. bump_daily_stats аддитивен и идемпотентен по дню (upsert), нулевой
     вызов не плодит строк;
  2. get_daily_summary ВЫВОДИТ исходы заказов из orders по created_at
     (today vs yesterday не смешиваются) и читает рантайм-счётчики.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, DailyStats, Order
from src.db.repo import _utc_day, bump_daily_stats, get_daily_summary
from src.timeutil import utcnow


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _order(oid: str, status: str, *, created_at) -> Order:
    return Order(
        funpay_order_id=oid,
        funpay_lot_id=1,
        ns_service_id=1,
        status=status,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_bump_accumulates_within_day(session):
    await bump_daily_stats(session, r429=2, exhausted=1)
    await bump_daily_stats(session, r429=3, deactivations=5)
    await session.commit()

    summary = await get_daily_summary(session)
    assert summary["r429"] == 5
    assert summary["exhausted"] == 1
    assert summary["deactivations"] == 5
    # ровно одна строка на день (upsert, не дубль)
    rows = (await session.execute(select(DailyStats))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_bump_zero_is_noop(session):
    await bump_daily_stats(session, r429=0, exhausted=0, deactivations=0)
    await session.commit()
    rows = (await session.execute(select(DailyStats))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_summary_derives_order_outcomes_for_today(session):
    now = utcnow()
    session.add_all([
        _order("a", "delivered", created_at=now),
        _order("b", "delivered", created_at=now),
        _order("c", "failed", created_at=now),
        _order("d", "manual_hold", created_at=now),
        _order("e", "pins_ready", created_at=now),
        _order("f", "received", created_at=now),
    ])
    await session.commit()

    s = await get_daily_summary(session)
    assert s["orders_ok"] == 2
    assert s["orders_failed"] == 1
    assert s["manual_holds"] == 1
    assert s["pins_ready"] == 1
    assert s["orders_total"] == 6


@pytest.mark.asyncio
async def test_summary_excludes_other_days(session):
    now = utcnow()
    yesterday = now - timedelta(days=1)
    session.add_all([
        _order("today-ok", "delivered", created_at=now),
        _order("yest-ok", "delivered", created_at=yesterday),
        _order("yest-fail", "failed", created_at=yesterday),
    ])
    await session.commit()

    today = await get_daily_summary(session, day=_utc_day())
    assert today["orders_ok"] == 1
    assert today["orders_failed"] == 0

    yk = (now.date() - timedelta(days=1)).isoformat()
    yest = await get_daily_summary(session, day=yk)
    assert yest["orders_ok"] == 1
    assert yest["orders_failed"] == 1


@pytest.mark.asyncio
async def test_summary_empty_day_is_zeros(session):
    s = await get_daily_summary(session, day="2000-01-01")
    assert s["orders_total"] == 0
    assert s["r429"] == 0
    assert s["deactivations"] == 0
