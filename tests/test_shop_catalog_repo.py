"""
Тесты shop/repo.py для catalog-функций:
- upsert_catalog_service (insert + update);
- enabled-флаг не перетирается upsert'ом (если оператор выключил услугу);
- mark_services_unseen: in_stock=0 для исчезнувших, защита от mass-wipe;
- list_services_in_category: pagination, sort by price asc;
- get_catalog_service: возвращает None для disabled.

Группировка по base_name тестируется отдельно в test_shop_catalog_groups.py.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, ShopCatalogCache
from src.shop.repo import (
    get_catalog_service,
    list_services_in_category,
    mark_services_unseen,
    upsert_catalog_service,
)


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


# ─── upsert ─────────────────────────────────────────────────────────


async def test_upsert_insert(factory):
    async with factory() as s:
        row = await upsert_catalog_service(
            s,
            ns_service_id=10, category_id=1, category_name="Apple",
            service_name="Apple $5", ns_price_usd=5.0,
            rub_price_kopecks=39609, in_stock=100, fields_json=None,
        )
        await s.commit()
        assert row.ns_service_id == 10
        assert row.enabled is True


async def test_upsert_update_changes_price(factory):
    async with factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=10, category_id=1, category_name="Apple",
            service_name="Apple $5", ns_price_usd=5.0,
            rub_price_kopecks=39609, in_stock=100, fields_json=None,
        )
        await s.commit()
    async with factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=10, category_id=1, category_name="Apple",
            service_name="Apple $5", ns_price_usd=5.5,
            rub_price_kopecks=43570, in_stock=80, fields_json=None,
        )
        await s.commit()
        row = (
            await s.execute(
                select(ShopCatalogCache).where(ShopCatalogCache.ns_service_id == 10)
            )
        ).scalar_one()
        assert row.ns_price_usd == 5.5
        assert row.rub_price_kopecks == 43570
        assert row.in_stock == 80


async def test_upsert_preserves_enabled_flag(factory):
    """
    Если оператор отключил услугу через owner-бота (enabled=False),
    последующий upsert от catalog_sync не должен включать её обратно.
    """
    async with factory() as s:
        row = await upsert_catalog_service(
            s, ns_service_id=10, category_id=1, category_name="A",
            service_name="X", ns_price_usd=5.0, rub_price_kopecks=10000,
            in_stock=10, fields_json=None,
        )
        row.enabled = False  # operator disabled it
        await s.commit()
    async with factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=10, category_id=1, category_name="A",
            service_name="X", ns_price_usd=5.5, rub_price_kopecks=11000,
            in_stock=10, fields_json=None,
        )
        await s.commit()
        row = (
            await s.execute(
                select(ShopCatalogCache).where(ShopCatalogCache.ns_service_id == 10)
            )
        ).scalar_one()
        assert row.enabled is False  # remains off


# ─── mark_services_unseen ───────────────────────────────────────────


async def test_mark_unseen_zeros_stock(factory):
    async with factory() as s:
        for sid in (1, 2, 3):
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=1, category_name="A",
                service_name=f"S{sid}", ns_price_usd=1.0,
                rub_price_kopecks=10000, in_stock=10, fields_json=None,
            )
        await s.commit()
    async with factory() as s:
        # NS вернул только 1 и 2 — 3 должно стать OOS
        marked = await mark_services_unseen(s, seen_service_ids=[1, 2])
        await s.commit()
        assert marked == 1
        row3 = (
            await s.execute(
                select(ShopCatalogCache).where(ShopCatalogCache.ns_service_id == 3)
            )
        ).scalar_one()
        assert row3.in_stock == 0


async def test_mark_unseen_empty_seen_does_not_wipe(factory):
    """Пустой seen_set ≠ massive wipe (это network blip)."""
    async with factory() as s:
        for sid in (1, 2):
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=1, category_name="A",
                service_name=f"S{sid}", ns_price_usd=1.0,
                rub_price_kopecks=10000, in_stock=10, fields_json=None,
            )
        await s.commit()
    async with factory() as s:
        marked = await mark_services_unseen(s, seen_service_ids=[])
        await s.commit()
        assert marked == 0
        rows = (await s.execute(select(ShopCatalogCache))).scalars().all()
        assert all(r.in_stock == 10 for r in rows)


# ─── list_services_in_category ──────────────────────────────────────


async def test_list_services_sorted_by_price(factory):
    async with factory() as s:
        # Добавим в обратном по цене порядке, чтобы убедиться, что sort работает
        for sid, price_k in [(1, 50000), (2, 30000), (3, 40000)]:
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10, category_name="A",
                service_name=f"X{sid}", ns_price_usd=1.0,
                rub_price_kopecks=price_k, in_stock=10, fields_json=None,
            )
        await s.commit()
        rows, total = await list_services_in_category(s, category_id=10)
        assert total == 3
        assert [r.ns_service_id for r in rows] == [2, 3, 1]  # 30, 40, 50


async def test_list_services_pagination(factory):
    async with factory() as s:
        for sid in range(1, 11):
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10, category_name="A",
                service_name=f"X{sid}", ns_price_usd=1.0,
                rub_price_kopecks=sid * 1000, in_stock=10, fields_json=None,
            )
        await s.commit()
        rows, total = await list_services_in_category(
            s, category_id=10, limit=3, offset=3
        )
        assert total == 10
        assert len(rows) == 3
        assert [r.ns_service_id for r in rows] == [4, 5, 6]


# ─── get_catalog_service ────────────────────────────────────────────


async def test_get_service_returns_none_for_disabled(factory):
    async with factory() as s:
        row = await upsert_catalog_service(
            s, ns_service_id=99, category_id=10, category_name="A",
            service_name="X", ns_price_usd=1.0, rub_price_kopecks=10000,
            in_stock=10, fields_json=None,
        )
        row.enabled = False
        await s.commit()
        found = await get_catalog_service(s, ns_service_id=99)
        assert found is None


async def test_get_service_returns_row_for_enabled_even_if_oos(factory):
    """OOS не блокирует доступ к карточке через get_catalog_service —
    решение «показать как недоступный» принимает UI-слой."""
    async with factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=42, category_id=1, category_name="A",
            service_name="X", ns_price_usd=1.0, rub_price_kopecks=10000,
            in_stock=0, fields_json=None,
        )
        await s.commit()
        found = await get_catalog_service(s, ns_service_id=42)
        assert found is not None
        assert found.in_stock == 0
