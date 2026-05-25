"""
Регрессионные тесты — diff-cache НЕ должен бесконечно продлевать TTL.

Найденный 2026-05-25 баг (после ночи продаж на ~20 заказов):
* purchase на FunPay снижает сток ВНУТРЕННЕ (FunPay сам списывает,
  без нашего save_lot);
* наш `last_synced_stock=100` остался в БД от последнего успешного save_lot;
* NS-сток снизился на quantity, но `target = min(NS, cap=100) = 100`
  (cap всё ещё связывает);
* `_is_cache_hit` ВИДИТ что last_stock==target_stock==100 → cache hit;
* `cache_hits_to_refresh_at` ОБНОВЛЯЛ `last_synced_at = now`;
* TTL никогда не истекал → FunPay-сток 97 застревал, не возвращался к 100.

После фикса:
* `last_synced_at` НЕ обновляется при cache-hit (см. test_no_refresh_on_cache_hit);
* `last_synced_at` обновляется ТОЛЬКО при реальном save_lot или verified-no-action
  (этот путь уже покрыт существующими тестами);
* через TTL диф-cache «протухает» сам собой и заставит FunPay GET — где увидится
  расхождение 97 ≠ 100 и сток поднимется обратно;
* при обработке заказа `OrderProcessor` инвалидирует cache явно
  (`invalidate_mapping_cache_for_funpay_lot`) — отклик в течение ~30с,
  не дожидаясь TTL.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Mapping
from src.db.repo import (
    invalidate_mapping_cache_for_funpay_lot,
    update_mapping_last_synced,
    upsert_mapping,
)


@pytest.fixture()
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ─────────────── Регрессия: cache-hit НЕ освежает last_synced_at ───────────────


def test_no_refresh_of_last_synced_at_on_cache_hit():
    """
    Статический анализ кода: в run_sync_once НЕ должно быть кода,
    который обновляет `last_synced_at` для cache-hits.

    Конкретно: блок `cache_hits_to_refresh_at` (через который баг
    2026-05-25 продлевал TTL до бесконечности) должен быть удалён.

    Этот тест простой по сути: ищем в src/sync/stock_sync.py
    отсутствие переменной `cache_hits_to_refresh_at`. Любое её
    появление = регрессия.
    """
    src = Path(__file__).parent.parent / "src" / "sync" / "stock_sync.py"
    code = src.read_text(encoding="utf-8")
    assert "cache_hits_to_refresh_at" not in code, (
        "БАГ-РЕГРЕССИЯ: переменная `cache_hits_to_refresh_at` снова "
        "появилась в stock_sync.py. Она обновляет last_synced_at при "
        "cache-hit, из-за чего TTL никогда не истекает и расхождения "
        "со стоком FunPay (после продаж) не подхватываются. Этот баг "
        "стоил суток продаж 2026-05-25 — не возвращай его обратно."
    )


# ─────────────── Repo: invalidate_mapping_cache_for_funpay_lot ───────────────


@pytest.mark.asyncio
async def test_invalidate_mapping_cache_clears_last_synced_at(db_factory):
    """
    После вызова `invalidate_mapping_cache_for_funpay_lot(funpay_lot_id=X)`
    у этого mapping `last_synced_at` должен стать NULL.

    Это нужно вызывать из `OrderProcessor` после успешной выдачи
    заказа: следующий sync-цикл (≤30с) увидит NULL → cache miss →
    реальный FunPay GET → обнаружит расхождение 97 ≠ 100 → save_lot(100).
    """
    async with db_factory() as session:
        mapping = await upsert_mapping(
            session,
            funpay_lot_id=42,
            ns_service_id=99,
            enabled=True,
            label="test",
        )
        await session.commit()
        mid = mapping.id

    async with db_factory() as session:
        await update_mapping_last_synced(
            session, mapping_id=mid, price=100.0, stock=100, active=True,
        )
        await session.commit()

    async with db_factory() as session:
        m = (await session.execute(
            select(Mapping).where(Mapping.id == mid)
        )).scalar_one()
        assert m.last_synced_at is not None, "preset: last_synced_at должен быть заполнен"
        assert m.last_synced_stock == 100

    async with db_factory() as session:
        affected = await invalidate_mapping_cache_for_funpay_lot(
            session, funpay_lot_id=42
        )
        await session.commit()
    assert affected == 1, "invalidate должен сообщить 1 затронутую строку"

    async with db_factory() as session:
        m = (await session.execute(
            select(Mapping).where(Mapping.id == mid)
        )).scalar_one()
        assert m.last_synced_at is None, (
            "last_synced_at должен стать NULL → следующий sync "
            "пойдёт через FunPay GET (cache miss)"
        )
        # Значения price/stock/active остаются — это просто метаданные,
        # они не используются если last_synced_at == NULL.
        assert m.last_synced_stock == 100  # не трогаем


@pytest.mark.asyncio
async def test_invalidate_mapping_cache_unknown_lot_id_is_noop(db_factory):
    """
    Если для funpay_lot_id нет mapping — функция не должна падать,
    просто возвращает 0 (заказ от лота, который ещё не замапплен).
    """
    async with db_factory() as session:
        affected = await invalidate_mapping_cache_for_funpay_lot(
            session, funpay_lot_id=999_999
        )
        await session.commit()
    assert affected == 0


@pytest.mark.asyncio
async def test_invalidate_only_affects_target_mapping(db_factory):
    """
    Инвалидация ОДНОГО mapping не должна задевать соседние —
    иначе при одной продаже сбрасывались бы все 47 lots и
    sync-цикл получил бы массовый FunPay GET (rate-limit-провокация).
    """
    async with db_factory() as session:
        m1 = await upsert_mapping(
            session, funpay_lot_id=111, ns_service_id=1, enabled=True, label="a",
        )
        m2 = await upsert_mapping(
            session, funpay_lot_id=222, ns_service_id=2, enabled=True, label="b",
        )
        await session.commit()
        mid1, mid2 = m1.id, m2.id

    async with db_factory() as session:
        for mid in (mid1, mid2):
            await update_mapping_last_synced(
                session, mapping_id=mid, price=100, stock=50, active=True,
            )
        await session.commit()

    async with db_factory() as session:
        await invalidate_mapping_cache_for_funpay_lot(session, funpay_lot_id=111)
        await session.commit()

    async with db_factory() as session:
        result = await session.execute(select(Mapping).order_by(Mapping.id))
        by_id = {m.id: m for m in result.scalars().all()}
    assert by_id[mid1].last_synced_at is None, "лот 111 — инвалидирован"
    assert by_id[mid2].last_synced_at is not None, "лот 222 — не должен быть тронут"
