"""
P0-1 Фаза B (этап B1): фундамент snapshot-sync — Mapping.funpay_node_id.

Проверяем repo-механику backfill:
  1. upsert_mapping пишет funpay_node_id, когда передан;
  2. upsert_mapping с funpay_node_id=None НЕ затирает уже известный node
     (иначе обычный upsert стёр бы backfill);
  3. set_mapping_node_id обновляет node по lot_id;
  4. list_mappings_missing_node_id возвращает только маппинги без node.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Mapping
from src.db.repo import (
    list_mappings_missing_node_id,
    set_mapping_node_id,
    upsert_mapping,
)


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_sets_node_id_when_given(session):
    await upsert_mapping(
        session, funpay_lot_id=1, ns_service_id=10, funpay_node_id=1316
    )
    await session.commit()
    m = (await session.execute(select(Mapping))).scalar_one()
    assert m.funpay_node_id == 1316


@pytest.mark.asyncio
async def test_upsert_none_does_not_clobber_existing_node(session):
    await upsert_mapping(
        session, funpay_lot_id=1, ns_service_id=10, funpay_node_id=1316
    )
    await session.commit()
    # повторный upsert БЕЗ node (обычное обновление label) не должен стереть
    await upsert_mapping(
        session, funpay_lot_id=1, ns_service_id=10, label="new label"
    )
    await session.commit()
    m = (await session.execute(select(Mapping))).scalar_one()
    assert m.funpay_node_id == 1316
    assert m.label == "new label"


@pytest.mark.asyncio
async def test_set_mapping_node_id(session):
    await upsert_mapping(session, funpay_lot_id=2, ns_service_id=20)
    await session.commit()

    ok = await set_mapping_node_id(session, funpay_lot_id=2, funpay_node_id=999)
    await session.commit()
    assert ok is True
    m = (await session.execute(select(Mapping))).scalar_one()
    assert m.funpay_node_id == 999

    missing = await set_mapping_node_id(
        session, funpay_lot_id=12345, funpay_node_id=1
    )
    assert missing is False


@pytest.mark.asyncio
async def test_list_mappings_missing_node_id(session):
    await upsert_mapping(session, funpay_lot_id=1, ns_service_id=10, funpay_node_id=5)
    await upsert_mapping(session, funpay_lot_id=2, ns_service_id=20)  # без node
    await upsert_mapping(session, funpay_lot_id=3, ns_service_id=30)  # без node
    await session.commit()

    missing = await list_mappings_missing_node_id(session)
    assert {m.funpay_lot_id for m in missing} == {2, 3}
