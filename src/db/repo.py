"""Высокоуровневые операции с БД."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import FxRate, Mapping, Order, SyncRun


# ---------- Mappings ----------

async def list_mappings(session: AsyncSession, *, only_enabled: bool = True) -> list[Mapping]:
    stmt = select(Mapping)
    if only_enabled:
        stmt = stmt.where(Mapping.enabled.is_(True))
    stmt = stmt.order_by(Mapping.funpay_lot_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def upsert_mapping(
    session: AsyncSession,
    *,
    funpay_lot_id: int,
    ns_service_id: int,
    markup_percent: float | None = None,
    stock_cap: int | None = None,
    ns_fields_template: str | None = None,
    enabled: bool = True,
    label: str | None = None,
) -> Mapping:
    existing = await session.execute(
        select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
    )
    obj = existing.scalar_one_or_none()
    if obj is None:
        obj = Mapping(funpay_lot_id=funpay_lot_id, ns_service_id=ns_service_id)
        session.add(obj)
    obj.ns_service_id = ns_service_id
    obj.markup_percent = markup_percent
    obj.stock_cap = stock_cap
    obj.ns_fields_template = ns_fields_template
    obj.enabled = enabled
    obj.label = label
    await session.flush()
    return obj


# ---------- FX rates ----------

async def save_fx_rate(
    session: AsyncSession, *, pair: str, rate: float, source: str | None = None
) -> FxRate:
    obj = FxRate(pair=pair, rate=rate, source=source)
    session.add(obj)
    await session.flush()
    return obj


async def latest_fx_rate(session: AsyncSession, pair: str) -> FxRate | None:
    stmt = select(FxRate).where(FxRate.pair == pair).order_by(FxRate.fetched_at.desc()).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ---------- Sync runs ----------

async def start_sync_run(session: AsyncSession) -> SyncRun:
    obj = SyncRun()
    session.add(obj)
    await session.flush()
    return obj


async def finish_sync_run(
    session: AsyncSession,
    run: SyncRun,
    *,
    status: str,
    lots_checked: int = 0,
    lots_updated: int = 0,
    lots_skipped: int = 0,
    error: str | None = None,
) -> None:
    run.finished_at = datetime.utcnow()
    run.status = status
    run.lots_checked = lots_checked
    run.lots_updated = lots_updated
    run.lots_skipped = lots_skipped
    run.error = error
    await session.flush()


# ---------- Orders ----------

async def find_order_by_funpay_id(session: AsyncSession, funpay_order_id: str) -> Order | None:
    stmt = select(Order).where(Order.funpay_order_id == funpay_order_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_order(
    session: AsyncSession,
    *,
    funpay_order_id: str,
    funpay_lot_id: int,
    ns_service_id: int,
    buyer_username: str | None,
    buyer_user_id: int | None,
    chat_id: int | None,
    quantity: int,
    funpay_price_rub: float | None,
) -> Order:
    obj = Order(
        funpay_order_id=funpay_order_id,
        funpay_lot_id=funpay_lot_id,
        ns_service_id=ns_service_id,
        buyer_username=buyer_username,
        buyer_user_id=buyer_user_id,
        chat_id=chat_id,
        quantity=quantity,
        funpay_price_rub=funpay_price_rub,
        status="received",
    )
    session.add(obj)
    await session.flush()
    return obj


async def update_order(session: AsyncSession, order: Order, **fields: Any) -> Order:
    for key, value in fields.items():
        if key == "pins":
            order.pins_json = json.dumps(value, ensure_ascii=False) if value is not None else None
        else:
            setattr(order, key, value)
    await session.flush()
    return order
