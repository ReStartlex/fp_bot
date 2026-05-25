"""
Тесты shop/repo.py для catalog-функций:
- upsert_catalog_service (insert + update);
- enabled-флаг не перетирается upsert'ом (если оператор выключил услугу);
- list_categories_for_ui: группировка, фильтр по in_stock>0 и enabled;
- list_services_in_category: pagination, sort by price asc;
- get_catalog_service: возвращает None для disabled.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, ShopCatalogCache
from src.shop.repo import (
    get_catalog_service,
    list_categories_for_ui,
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


# ─── list_categories_for_ui ─────────────────────────────────────────


async def test_list_categories_groups_and_filters(factory):
    async with factory() as s:
        # Apple: 2 in-stock, 1 oos
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10, category_name="Apple",
            service_name="A1", ns_price_usd=5.0, rub_price_kopecks=50000,
            in_stock=100, fields_json=None,
        )
        await upsert_catalog_service(
            s, ns_service_id=2, category_id=10, category_name="Apple",
            service_name="A2", ns_price_usd=10.0, rub_price_kopecks=80000,
            in_stock=50, fields_json=None,
        )
        oos_row = await upsert_catalog_service(
            s, ns_service_id=3, category_id=10, category_name="Apple",
            service_name="A3", ns_price_usd=15.0, rub_price_kopecks=120000,
            in_stock=0, fields_json=None,  # OOS — не должен попасть
        )
        # Steam: 1 in-stock, 1 disabled
        await upsert_catalog_service(
            s, ns_service_id=4, category_id=20, category_name="Steam",
            service_name="St1", ns_price_usd=20.0, rub_price_kopecks=150000,
            in_stock=10, fields_json=None,
        )
        disabled_row = await upsert_catalog_service(
            s, ns_service_id=5, category_id=20, category_name="Steam",
            service_name="St2", ns_price_usd=25.0, rub_price_kopecks=180000,
            in_stock=10, fields_json=None,
        )
        disabled_row.enabled = False
        # Spotify: вообще все OOS — категория не должна попасть
        await upsert_catalog_service(
            s, ns_service_id=6, category_id=30, category_name="Spotify",
            service_name="Sp1", ns_price_usd=5.0, rub_price_kopecks=40000,
            in_stock=0, fields_json=None,
        )
        await s.commit()

        cats = await list_categories_for_ui(s)
        names = [c.category_name for c in cats]
        assert names == ["Apple", "Steam"]  # отсортировано по имени
        apple = next(c for c in cats if c.category_name == "Apple")
        assert apple.services_count == 2  # OOS не считается
        assert apple.cheapest_price_kopecks == 50000
        steam = next(c for c in cats if c.category_name == "Steam")
        assert steam.services_count == 1  # disabled не считается


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
