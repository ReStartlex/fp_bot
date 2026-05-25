"""
Тесты воркера shop_catalog_sync:
- happy path: 2 категории × 3 сервиса → 6 строк в shop_catalog_cache,
  цены корректно посчитаны (markup 8% + fx);
- идемпотентность: повторный run без изменений NS — те же значения;
- частичное обновление цен: NS снизил цену → cache подхватывает;
- исчезновение service_id из NS → in_stock=0 (не удаление);
- пустой ответ NS (network blip) → cache НЕ обнуляется;
- NS exception → cache не трогается, ошибка пробрасывается, sync возвращает
  status=failed (не вешает планировщик).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base, ShopCatalogCache
from src.ns.models import Category, FieldType, Service, StockResponse
from src.shop.catalog_sync import sync_catalog_once
from src.sync.fx import RateBreakdown


@pytest.fixture()
async def factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.shop.catalog_sync.session_factory", lambda: f)
    yield f
    await engine.dispose()


def _settings(**overrides):
    base: dict = dict(
        ns_user_id=1, ns_login="x", ns_password="x", ns_api_secret="QQ==",
        funpay_golden_key="x", funpay_user_id=1,
        shop_enabled=True,
        shop_telegram_bot_token="dummy",
        shop_markup_percent=8.0,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


def _stock(num_categories: int = 1, services_per_cat: int = 3) -> StockResponse:
    cats = []
    sid_counter = 100
    for ci in range(num_categories):
        services = []
        for si in range(services_per_cat):
            services.append(Service(
                service_id=sid_counter,
                service_name=f"Card #{sid_counter}",
                price=float(sid_counter) / 100.0,  # 1.0, 1.01, ...
                currency="USD",
                in_stock=50,
            ))
            sid_counter += 1
        cats.append(Category(
            category_id=ci + 1,
            category_name=f"Category {ci + 1}",
            services=services,
            fields=[FieldType(key="quantity", type="number", name="Quantity", required=True)],
        ))
    return StockResponse(categories=cats)


class FakeNS:
    """Минимальная замена NSClient.get_stock."""
    def __init__(self, stock: StockResponse | None = None, raises: Exception | None = None):
        self._stock = stock
        self._raises = raises
        self.calls = 0

    async def get_stock(self) -> StockResponse:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._stock is not None
        return self._stock


async def _fake_breakdown(*args, **kwargs) -> RateBreakdown:
    return RateBreakdown(base=70.0, premium_percent=3.0, effective=72.10, source="cbr")


# ─────────────── happy path ───────────────


async def test_sync_creates_catalog_entries(factory, monkeypatch):
    monkeypatch.setattr(
        "src.shop.catalog_sync.get_rate_breakdown", _fake_breakdown
    )
    ns = FakeNS(_stock(num_categories=2, services_per_cat=3))
    result = await sync_catalog_once(ns_client=ns, settings=_settings())

    assert result["status"] == "ok"
    assert result["fetched"] == 6
    assert result["upserted"] == 6
    assert result["fx_rate"] == pytest.approx(72.10)

    async with factory() as s:
        rows = (await s.execute(select(ShopCatalogCache))).scalars().all()
        assert len(rows) == 6
        # Для service_id=100: ns_price=1.00$ × 72.10 × 1.08 = 77.868₽ → 7787 коп
        sid100 = next(r for r in rows if r.ns_service_id == 100)
        assert sid100.rub_price_kopecks == 7787
        assert sid100.ns_price_usd == pytest.approx(1.0)
        assert sid100.in_stock == 50
        assert sid100.category_name == "Category 1"
        assert sid100.fields_json is not None  # JSON со схемой полей


async def test_sync_is_idempotent(factory, monkeypatch):
    """Два прогона подряд при том же ответе NS — состояние БД одинаковое."""
    monkeypatch.setattr(
        "src.shop.catalog_sync.get_rate_breakdown", _fake_breakdown
    )
    ns = FakeNS(_stock())
    r1 = await sync_catalog_once(ns_client=ns, settings=_settings())
    r2 = await sync_catalog_once(ns_client=ns, settings=_settings())

    assert r1["upserted"] == 3
    assert r2["upserted"] == 3  # снова upsert, но значения те же

    async with factory() as s:
        rows = (await s.execute(select(ShopCatalogCache))).scalars().all()
        assert len(rows) == 3


async def test_sync_updates_price_when_changed(factory, monkeypatch):
    monkeypatch.setattr(
        "src.shop.catalog_sync.get_rate_breakdown", _fake_breakdown
    )
    # 1-й прогон: цена 1.00$
    ns = FakeNS(_stock())
    await sync_catalog_once(ns_client=ns, settings=_settings())

    # 2-й прогон: цена 2.00$ для service_id=100
    s2 = _stock()
    s2.categories[0].services[0].price = 2.0
    ns2 = FakeNS(s2)
    await sync_catalog_once(ns_client=ns2, settings=_settings())

    async with factory() as s:
        sid100 = (
            await s.execute(
                select(ShopCatalogCache).where(ShopCatalogCache.ns_service_id == 100)
            )
        ).scalar_one()
        # 2.00 * 72.10 * 1.08 = 155.736 → 15574 коп
        assert sid100.rub_price_kopecks == 15574
        assert sid100.ns_price_usd == pytest.approx(2.0)


async def test_sync_marks_disappeared_service_oos(factory, monkeypatch):
    """NS убрал service_id из ответа → in_stock=0, но запись остаётся."""
    monkeypatch.setattr(
        "src.shop.catalog_sync.get_rate_breakdown", _fake_breakdown
    )
    # 1-й прогон с 3 сервисами
    await sync_catalog_once(ns_client=FakeNS(_stock()), settings=_settings())

    # 2-й прогон — только 2 сервиса (100 исчез)
    s2 = _stock()
    s2.categories[0].services = s2.categories[0].services[1:]  # без sid=100
    result = await sync_catalog_once(ns_client=FakeNS(s2), settings=_settings())

    assert result["marked_oos"] == 1
    async with factory() as s:
        sid100 = (
            await s.execute(
                select(ShopCatalogCache).where(ShopCatalogCache.ns_service_id == 100)
            )
        ).scalar_one()
        # Запись осталась, но in_stock=0
        assert sid100.in_stock == 0


# ─────────────── error handling ───────────────


async def test_sync_skips_zero_priced_services(factory, monkeypatch):
    """NS вернул услугу с price=0 — пропускаем (не добавляем в каталог)."""
    monkeypatch.setattr(
        "src.shop.catalog_sync.get_rate_breakdown", _fake_breakdown
    )
    stock = _stock()
    stock.categories[0].services[0].price = 0.0  # service_id=100 «бесплатный»

    result = await sync_catalog_once(
        ns_client=FakeNS(stock), settings=_settings()
    )
    assert result["status"] == "ok"
    assert result["skipped_invalid"] == 1
    assert result["upserted"] == 2

    async with factory() as s:
        rows = (await s.execute(select(ShopCatalogCache))).scalars().all()
        sids = {r.ns_service_id for r in rows}
        assert 100 not in sids
        assert 101 in sids
        assert 102 in sids


async def test_sync_handles_empty_ns_response(factory, monkeypatch):
    """Пустой каталог от NS — НЕ обнуляем cache (защита от network blip)."""
    monkeypatch.setattr(
        "src.shop.catalog_sync.get_rate_breakdown", _fake_breakdown
    )
    # Заполняем cache
    await sync_catalog_once(ns_client=FakeNS(_stock()), settings=_settings())

    # NS прислал пусто
    empty = StockResponse(categories=[])
    result = await sync_catalog_once(
        ns_client=FakeNS(empty), settings=_settings()
    )
    assert result["status"] == "empty_ns_response"
    assert result["fetched"] == 0

    async with factory() as s:
        rows = (await s.execute(select(ShopCatalogCache))).scalars().all()
        # Cache не тронут
        assert len(rows) == 3
        assert all(r.in_stock == 50 for r in rows)


async def test_sync_handles_ns_exception(factory, monkeypatch):
    """NS бросил исключение — sync помечается failed, cache не трогается."""
    monkeypatch.setattr(
        "src.shop.catalog_sync.get_rate_breakdown", _fake_breakdown
    )
    # Сначала заполним cache
    await sync_catalog_once(ns_client=FakeNS(_stock()), settings=_settings())

    # Теперь NS падает
    ns = FakeNS(raises=RuntimeError("NS API timeout"))
    result = await sync_catalog_once(ns_client=ns, settings=_settings())

    assert result["status"] == "failed"
    assert "timeout" in result["error"].lower()
    assert ns.calls == 1

    async with factory() as s:
        rows = (await s.execute(select(ShopCatalogCache))).scalars().all()
        assert len(rows) == 3  # cache цел


async def test_sync_uses_runtime_markup_override(factory, monkeypatch):
    """Runtime markup override через RuntimeSetting перебивает settings.shop_markup_percent."""
    monkeypatch.setattr(
        "src.shop.catalog_sync.get_rate_breakdown", _fake_breakdown
    )
    from src.config_runtime import set_shop_markup_percent

    # Override: 20% вместо дефолтных 8%
    async with factory() as s:
        pass  # просто чтобы init_db уже отработал
    # set_shop_markup_percent использует свой session_factory из src.db.session;
    # его мы тоже монкипатчим.
    monkeypatch.setattr(
        "src.config_runtime.session_factory", lambda: factory
    )
    await set_shop_markup_percent(20.0)

    await sync_catalog_once(ns_client=FakeNS(_stock()), settings=_settings())

    async with factory() as s:
        sid100 = (
            await s.execute(
                select(ShopCatalogCache).where(ShopCatalogCache.ns_service_id == 100)
            )
        ).scalar_one()
        # 1.00 * 72.10 * 1.20 = 86.52₽ → 8652 коп
        assert sid100.rub_price_kopecks == 8652
