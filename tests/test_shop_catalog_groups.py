"""
Тесты группировки каталога:
- list_category_groups_for_ui: свёртка региональных вариантов в группы;
- list_categories_in_group: drill-down в варианты конкретной группы;
- search_services: поиск по service_name и base_name.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base
from src.shop.repo import (
    list_categories_in_group,
    list_category_groups_for_ui,
    search_services,
    upsert_catalog_service,
)
from src.shop.taxonomy import make_group_slug


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


async def _seed_regional(factory):
    """
    Закидываем смешанный набор:
    - Apple Gift Card: US / EU / UK (3 региона, 1 услуга в каждом)
    - EA SPORTS FC™ 25 | GLOB | Xbox Games (1 кат, 2 услуги)
    - EA SPORTS FC™ Mobile (без |, 1 кат, 1 услуга)
    - Spotify | RU (нет в наличии, должен скрыться)
    """
    apple_slug = make_group_slug("Apple Gift Card")
    ea25_slug = make_group_slug("EA SPORTS FC™ 25")
    eam_slug = make_group_slug("EA SPORTS FC™ Mobile")
    spo_slug = make_group_slug("Spotify")

    async with factory() as s:
        # Apple — 3 региональных кат, 3 услуги (по 1 на регион)
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10, category_name="Apple Gift Card | US",
            service_name="Apple US $5", base_name="Apple Gift Card",
            group_slug=apple_slug, ns_price_usd=5, rub_price_kopecks=40000,
            in_stock=10, fields_json=None,
        )
        await upsert_catalog_service(
            s, ns_service_id=2, category_id=11, category_name="Apple Gift Card | EU",
            service_name="Apple EU €5", base_name="Apple Gift Card",
            group_slug=apple_slug, ns_price_usd=5.5, rub_price_kopecks=44000,
            in_stock=10, fields_json=None,
        )
        await upsert_catalog_service(
            s, ns_service_id=3, category_id=12, category_name="Apple Gift Card | UK",
            service_name="Apple UK £5", base_name="Apple Gift Card",
            group_slug=apple_slug, ns_price_usd=6, rub_price_kopecks=48000,
            in_stock=10, fields_json=None,
        )
        # EA 25 — 1 кат, 2 услуги
        await upsert_catalog_service(
            s, ns_service_id=4, category_id=20,
            category_name="EA SPORTS FC™ 25 | GLOB | Xbox Games",
            service_name="EA25 GOLD", base_name="EA SPORTS FC™ 25",
            group_slug=ea25_slug, ns_price_usd=20, rub_price_kopecks=160000,
            in_stock=5, fields_json=None,
        )
        await upsert_catalog_service(
            s, ns_service_id=5, category_id=20,
            category_name="EA SPORTS FC™ 25 | GLOB | Xbox Games",
            service_name="EA25 ULTIMATE", base_name="EA SPORTS FC™ 25",
            group_slug=ea25_slug, ns_price_usd=30, rub_price_kopecks=240000,
            in_stock=5, fields_json=None,
        )
        # EA Mobile — 1 кат, 1 услуга
        await upsert_catalog_service(
            s, ns_service_id=6, category_id=30, category_name="EA SPORTS FC™ Mobile",
            service_name="EA Mobile pack", base_name="EA SPORTS FC™ Mobile",
            group_slug=eam_slug, ns_price_usd=1, rub_price_kopecks=8000,
            in_stock=20, fields_json=None,
        )
        # Spotify — OOS, скрыт
        await upsert_catalog_service(
            s, ns_service_id=7, category_id=40, category_name="Spotify | RU",
            service_name="Spotify Premium", base_name="Spotify",
            group_slug=spo_slug, ns_price_usd=5, rub_price_kopecks=40000,
            in_stock=0, fields_json=None,
        )
        await s.commit()
    return apple_slug, ea25_slug, eam_slug


# ─── list_category_groups_for_ui ─────────────────────────────────────


async def test_groups_collapse_regional_variants(factory):
    apple_slug, ea25_slug, eam_slug = await _seed_regional(factory)
    async with factory() as s:
        groups = await list_category_groups_for_ui(s)

    bases = [g.base_name for g in groups]
    # Spotify не должен попасть — все OOS
    assert "Spotify" not in bases
    # Apple, EA 25, EA Mobile — 3 группы
    assert set(bases) == {
        "Apple Gift Card", "EA SPORTS FC™ 25", "EA SPORTS FC™ Mobile"
    }

    apple = next(g for g in groups if g.base_name == "Apple Gift Card")
    # 3 региональных category_id → variants_count=3
    assert apple.variants_count == 3
    # 3 услуги суммарно
    assert apple.services_count == 3
    # Самая дешёвая — 400₽ (40000 копеек)
    assert apple.cheapest_price_kopecks == 40000
    # slug совпадает
    assert apple.group_slug == apple_slug

    ea25 = next(g for g in groups if g.base_name == "EA SPORTS FC™ 25")
    # одна категория, две услуги
    assert ea25.variants_count == 1
    assert ea25.services_count == 2


async def test_groups_skips_records_without_slug(factory):
    """Legacy записи (без group_slug) НЕ попадают — будут обновлены при
    следующем sync'е."""
    async with factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=99, category_id=99, category_name="Legacy",
            service_name="Old", base_name=None, group_slug=None,
            ns_price_usd=1, rub_price_kopecks=8000, in_stock=10, fields_json=None,
        )
        await s.commit()
        groups = await list_category_groups_for_ui(s)
        assert groups == []


# ─── list_categories_in_group ───────────────────────────────────────


async def test_drill_down_into_apple_shows_3_regions(factory):
    apple_slug, _, _ = await _seed_regional(factory)
    async with factory() as s:
        variants = await list_categories_in_group(s, group_slug=apple_slug)
    names = [v.category_name for v in variants]
    # Алфавитно: EU, UK, US
    assert names == [
        "Apple Gift Card | EU",
        "Apple Gift Card | UK",
        "Apple Gift Card | US",
    ]
    # У каждого региона 1 услуга, самая дешёвая своя
    us = next(v for v in variants if v.category_name.endswith("| US"))
    assert us.services_count == 1
    assert us.cheapest_price_kopecks == 40000


async def test_drill_down_unknown_slug_returns_empty(factory):
    await _seed_regional(factory)
    async with factory() as s:
        result = await list_categories_in_group(s, group_slug="0000000000")
        assert result == []


# ─── search_services ────────────────────────────────────────────────


async def test_search_by_service_name(factory):
    await _seed_regional(factory)
    async with factory() as s:
        results = await search_services(s, query="apple")
    names = [r.service_name for r in results]
    # Найдены все 3 Apple
    assert len(results) == 3
    assert all("Apple" in n for n in names)
    # Отсортированы по цене (US 40000, EU 44000, UK 48000)
    assert [r.rub_price_kopecks for r in results] == [40000, 44000, 48000]


async def test_search_by_base_name(factory):
    await _seed_regional(factory)
    async with factory() as s:
        results = await search_services(s, query="ea sports")
    # EA SPORTS FC™ 25 (2 услуги) + EA SPORTS FC™ Mobile (1)
    assert len(results) == 3
    # EA Mobile дешевле всего (8000 коп) — первый
    assert results[0].ns_service_id == 6


async def test_search_too_short_returns_empty(factory):
    await _seed_regional(factory)
    async with factory() as s:
        assert await search_services(s, query="") == []
        assert await search_services(s, query="a") == []
        assert await search_services(s, query=" ") == []


async def test_search_no_matches(factory):
    await _seed_regional(factory)
    async with factory() as s:
        assert await search_services(s, query="nonexistent_zzz") == []


async def test_search_excludes_oos(factory):
    """OOS-услуги не возвращаются (даже если matches by name)."""
    await _seed_regional(factory)
    async with factory() as s:
        results = await search_services(s, query="spotify")
        assert results == []


async def test_search_is_case_insensitive(factory):
    await _seed_regional(factory)
    async with factory() as s:
        upper = await search_services(s, query="APPLE")
        lower = await search_services(s, query="apple")
        assert [r.ns_service_id for r in upper] == [r.ns_service_id for r in lower]
