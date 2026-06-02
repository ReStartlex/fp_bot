"""Тесты batch enable/disable всех замапленных лотов.

См. src/sync/lots_control.py — кнопки «🔴 Выключить все» / «🟢 Включить все»
в Telegram-боте.

Покрываем:
  1. disable_all: mapping.enabled = False для всех; save_lot вызывается
     с active=False, amount=0; rate-limit delay = 0 в тестах.
  2. disable_all: уже dead лоты (active=False, amount=0) → не save_lot,
     funpay_already += 1.
  3. disable_all: save_lot падает на части лотов → errors=N, остальные
     обрабатываются, mapping.enabled всё равно = False (для всех).
  4. disable_all: funpay_client=None → только БД, save_lot не зовётся.
  5. enable_all: mapping.enabled = True для всех (даже disabled ранее);
     last_synced_at = NULL для всех (cache invalidation).
  6. enable_all: НЕ дёргает save_lot (sync_stock сам поднимет лоты).
  7. Пустая БД → total=0, никаких side effects.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Mapping
from src.db.repo import upsert_mapping
from src.sync.lots_control import (
    BatchLotResult,
    disable_all_mapped_lots,
    enable_all_mapped_lots,
)


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.sync.lots_control.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


class _FakeLot:
    def __init__(self, lot_id: int, *, active: bool = True, amount: int = 99):
        self.lot_id = lot_id
        self.active = active
        self.amount = amount


class _FakeFP:
    def __init__(self, lots: dict[int, _FakeLot] | None = None,
                 fail_on_save: set[int] | None = None,
                 fail_on_get: set[int] | None = None):
        self.lots = lots or {}
        self.fail_on_save = fail_on_save or set()
        self.fail_on_get = fail_on_get or set()
        self.save_calls: list[tuple[int, bool, int]] = []

    async def get_lot_fields(self, lot_id: int):
        if lot_id in self.fail_on_get:
            raise RuntimeError(f"GET {lot_id} failed")
        if lot_id not in self.lots:
            self.lots[lot_id] = _FakeLot(lot_id, active=True, amount=99)
        return self.lots[lot_id]

    async def save_lot(self, lot_fields):
        lot_id = getattr(lot_fields, "lot_id", -1)
        if lot_id in self.fail_on_save:
            raise RuntimeError(f"save {lot_id} failed")
        self.save_calls.append((lot_id, lot_fields.active, lot_fields.amount))
        return {"ok": True}


async def _seed(db_factory, mappings: list[tuple[int, int, bool]]):
    """Заливаем (lot_id, service_id, enabled) для теста."""
    async with db_factory() as session:
        for lot_id, svc_id, enabled in mappings:
            await upsert_mapping(
                session,
                funpay_lot_id=lot_id,
                ns_service_id=svc_id,
                enabled=enabled,
            )
        await session.commit()


# ─────────────────────────── disable_all ─────────────────────────────────


@pytest.mark.asyncio
async def test_disable_all_marks_db_and_calls_save_lot(db_factory):
    await _seed(db_factory, [(100, 1, True), (200, 2, True), (300, 3, True)])
    fp = _FakeFP()

    result = await disable_all_mapped_lots(
        funpay_client=fp, inter_request_delay_seconds=0
    )

    assert result.total == 3
    assert result.db_updated == 3
    assert result.funpay_changed == 3
    assert result.errors == 0

    # БД: все enabled = False
    async with db_factory() as session:
        rows = (await session.execute(select(Mapping))).scalars().all()
        assert all(not m.enabled for m in rows)

    # FunPay: save_lot вызван с active=False, amount=0
    assert sorted(fp.save_calls) == [(100, False, 0), (200, False, 0), (300, False, 0)]


@pytest.mark.asyncio
async def test_disable_all_skips_already_dead(db_factory):
    await _seed(db_factory, [(10, 1, True), (20, 2, True)])
    fp = _FakeFP(lots={
        10: _FakeLot(10, active=False, amount=0),   # уже dead
        20: _FakeLot(20, active=True, amount=5),    # требует save_lot
    })

    result = await disable_all_mapped_lots(
        funpay_client=fp, inter_request_delay_seconds=0
    )

    assert result.funpay_already == 1
    assert result.funpay_changed == 1
    assert fp.save_calls == [(20, False, 0)]


@pytest.mark.asyncio
async def test_disable_all_continues_on_save_failure(db_factory):
    await _seed(db_factory, [(1, 1, True), (2, 2, True), (3, 3, True)])
    fp = _FakeFP(fail_on_save={2})

    result = await disable_all_mapped_lots(
        funpay_client=fp, inter_request_delay_seconds=0
    )

    assert result.total == 3
    assert result.funpay_changed == 2  # 1 и 3 успешно
    assert result.errors == 1
    assert 2 in result.error_lot_ids

    # В БД все равно все enabled=False (включая failed на save_lot)
    async with db_factory() as session:
        rows = (await session.execute(select(Mapping))).scalars().all()
        assert all(not m.enabled for m in rows)


@pytest.mark.asyncio
async def test_disable_all_handles_get_failure(db_factory):
    await _seed(db_factory, [(1, 1, True), (2, 2, True)])
    fp = _FakeFP(fail_on_get={1})

    result = await disable_all_mapped_lots(
        funpay_client=fp, inter_request_delay_seconds=0
    )

    assert result.errors == 1
    assert 1 in result.error_lot_ids
    assert result.funpay_changed == 1  # лот 2 успешно


@pytest.mark.asyncio
async def test_disable_all_no_funpay_client_db_only(db_factory):
    await _seed(db_factory, [(7, 7, True), (8, 8, True)])

    result = await disable_all_mapped_lots(funpay_client=None)

    assert result.total == 2
    assert result.db_updated == 2
    assert result.funpay_changed == 0
    assert result.errors == 0

    async with db_factory() as session:
        rows = (await session.execute(select(Mapping))).scalars().all()
        assert all(not m.enabled for m in rows)


@pytest.mark.asyncio
async def test_disable_all_empty_db(db_factory):
    fp = _FakeFP()
    result = await disable_all_mapped_lots(
        funpay_client=fp, inter_request_delay_seconds=0
    )
    assert result == BatchLotResult()
    assert fp.save_calls == []


# ─────────────────────────── enable_all ──────────────────────────────────


@pytest.mark.asyncio
async def test_enable_all_marks_db_and_invalidates_cache(db_factory):
    """Включаем все: даже те, что были disabled, должны стать enabled.

    Также проверяем, что last_synced_at = NULL для всех (это критично:
    diff-cache мог пометить их «свежими» и sync_stock пропустит).
    """
    await _seed(db_factory, [(1, 1, False), (2, 2, False), (3, 3, True)])
    # Имитируем что у части маппингов last_synced_at недавний
    async with db_factory() as session:
        for m in (await session.execute(select(Mapping))).scalars().all():
            m.last_synced_at = datetime.utcnow() - timedelta(seconds=10)
        await session.commit()

    fp = _FakeFP()
    result = await enable_all_mapped_lots(funpay_client=fp)

    assert result.total == 3
    assert result.db_updated == 3
    assert result.errors == 0

    async with db_factory() as session:
        rows = (await session.execute(select(Mapping))).scalars().all()
        assert all(m.enabled for m in rows)
        # last_synced_at = NULL у всех — sync_stock на след тике увидит miss
        assert all(m.last_synced_at is None for m in rows)


@pytest.mark.asyncio
async def test_enable_all_does_not_call_save_lot(db_factory):
    """enable_all только готовит БД; save_lot делает sync_stock."""
    await _seed(db_factory, [(1, 1, False), (2, 2, False)])
    fp = _FakeFP()

    await enable_all_mapped_lots(funpay_client=fp)

    assert fp.save_calls == []


@pytest.mark.asyncio
async def test_enable_all_empty_db(db_factory):
    fp = _FakeFP()
    result = await enable_all_mapped_lots(funpay_client=fp)
    assert result == BatchLotResult()
