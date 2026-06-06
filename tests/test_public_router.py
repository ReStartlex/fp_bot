"""
Тесты публичного API витрины neurodrop.ru (`/api/public/*`).

Ключевое отличие от Mini App (`test_shop_router.py`): эндпоинты НЕ требуют
авторизации (их зовёт SSR-фронт и поисковые роботы). Проверяем:
  * доступ без X-Telegram-Init-Data;
  * группы / варианты / список услуг / карточка / поиск;
  * 404 на отсутствующий и распроданный товар;
  * счётчики (/stats) и /sitemap;
  * наличие Cache-Control (важно для CDN/SEO);
  * витрина не раскрывает закупочную цену (ns_price_usd).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.app import create_app
from src.db.models import Base
from src.shop.repo import upsert_catalog_service
from src.shop.taxonomy import make_group_slug


@pytest.fixture()
def app_settings(monkeypatch):
    monkeypatch.setenv("FUNPAY_GOLDEN_KEY", "x" * 64)
    monkeypatch.setenv("FUNPAY_CURRENCY", "RUB")
    monkeypatch.setenv("SHOP_ENABLED", "true")
    monkeypatch.setenv("SHOP_TELEGRAM_BOT_TOKEN", "1234567890:PUBLIC_TEST_TOKEN_AAA")
    import src.config as cfg
    monkeypatch.setattr(cfg, "_settings", None)
    yield


@pytest.fixture()
async def db_factory(monkeypatch, app_settings):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def fake_factory():
        return factory

    monkeypatch.setattr("src.api.public_router.session_factory", fake_factory)
    yield factory
    await engine.dispose()


@pytest.fixture()
def client(app_settings):
    return TestClient(create_app())


async def _seed_apple(factory) -> None:
    """Засеивает три номинала Apple US в один бренд/категорию."""
    slug = make_group_slug("Apple Gift Card")
    async with factory() as s:
        for sid, name, price, stock in (
            (1, "Apple US $5", 40000, 10),
            (2, "Apple US $10", 80000, 7),
            (3, "Apple US $25", 200000, 0),  # распродан
        ):
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10,
                category_name="Apple Gift Card | US",
                service_name=name,
                base_name="Apple Gift Card", group_slug=slug,
                ns_price_usd=price / 8000, rub_price_kopecks=price,
                in_stock=stock, fields_json=None,
            )
        await s.commit()


# ─── No-auth access ────────────────────────────────────────────────


async def test_groups_no_auth_required(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/catalog/groups")  # без заголовков
    assert resp.status_code == 200, resp.text
    groups = resp.json()
    assert any(g["base_name"] == "Apple Gift Card" for g in groups)
    apple = next(g for g in groups if g["base_name"] == "Apple Gift Card")
    # распроданный $25 не считается, дешёвый — $5
    assert apple["cheapest_price_kopecks"] == 40000
    assert apple["services_count"] == 2


async def test_groups_sets_cache_control(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/catalog/groups")
    assert "max-age" in resp.headers.get("cache-control", "")


# ─── Drill-down ────────────────────────────────────────────────────


async def test_group_variants(client, db_factory):
    await _seed_apple(db_factory)
    slug = make_group_slug("Apple Gift Card")
    resp = client.get(f"/api/public/catalog/groups/{slug}")
    assert resp.status_code == 200
    variants = resp.json()
    assert len(variants) == 1
    assert variants[0]["category_name"] == "Apple Gift Card | US"
    assert variants[0]["services_count"] == 2


async def test_category_services_pagination(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/catalog/categories/10?page=0&page_size=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2  # только в наличии
    assert len(body["items"]) == 1
    assert body["items"][0]["rub_price_kopecks"] == 40000  # сортировка по цене


# ─── Service card ──────────────────────────────────────────────────


async def test_service_card_with_similar(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/catalog/services/1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ns_service_id"] == 1
    assert body["service_name"] == "Apple US $5"
    # похожие — другой номинал того же бренда в наличии ($10), без $5 и без $25
    similar_ids = {s["ns_service_id"] for s in body["similar"]}
    assert 2 in similar_ids
    assert 1 not in similar_ids
    assert 3 not in similar_ids


async def test_service_card_does_not_leak_usd_cost(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/catalog/services/1")
    assert resp.status_code == 200
    assert "ns_price_usd" not in resp.json()


async def test_service_card_404_for_missing(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/catalog/services/99999")
    assert resp.status_code == 404


async def test_service_card_404_for_out_of_stock(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/catalog/services/3")  # $25, in_stock=0
    assert resp.status_code == 404


# ─── Search ────────────────────────────────────────────────────────


async def test_search_finds_apple(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/search", params={"q": "apple"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "apple"
    assert len(body["items"]) == 2  # только в наличии


async def test_search_short_query_returns_empty(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/search", params={"q": "a"})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# ─── Stats & sitemap ───────────────────────────────────────────────


async def test_stats(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["products_in_stock"] == 2
    assert body["groups_count"] == 1
    assert body["categories_count"] == 1
    assert body["updated_at"] is not None


async def test_sitemap_lists_only_active(client, db_factory):
    await _seed_apple(db_factory)
    resp = client.get("/api/public/sitemap")
    assert resp.status_code == 200
    body = resp.json()
    ids = {e["ns_service_id"] for e in body["services"]}
    assert ids == {1, 2}  # распроданный $25 не попадает
    assert make_group_slug("Apple Gift Card") in body["groups"]
