"""
Тесты автономной миграции платформы (config + orchestrator).
"""
from __future__ import annotations

import textwrap

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Mapping
from src.funpay.admin_http import LotFields
from src.migrate.auto import migrate_platform
from src.migrate.config import get_platform, load_config, resolve_fields
from src.ns.models import Category, FieldType, Service


# ───────────── config ─────────────

def _write_config(tmp_path) -> str:
    p = tmp_path / "migration.yaml"
    p.write_text(textwrap.dedent("""
    platforms:
      - name: Steam
        ns_grep: "steam wallet"
        funpay_node: 1086
        markup_percent: 5
        fields:
          "fields[currency]": "{currency}"
          "fields[type]": "Подарочная карта"
          "fields[quantity]": "{nominal}"
      - name: Apple
        ns_grep: "apple"
        funpay_node: 1316
        markup_percent: 7
        schema_offer: 69405880
        by_currency:
          USD: { "fields[currency]": "USD", "fields[usd]": "{nominal} USD" }
        by_category:
          5: { "fields[currency]": "TRY-SPECIAL", "fields[try]": "{nominal} TRY" }
    """), encoding="utf-8")
    return str(p)


def test_load_and_resolve_fields(tmp_path):
    cfg = load_config(_write_config(tmp_path))
    steam = get_platform(cfg, "steam")
    assert steam is not None and steam.funpay_node == 1086
    # Steam: общий fields для любой валюты
    assert resolve_fields(steam, 2, "USD")["fields[type]"] == "Подарочная карта"

    apple = get_platform(cfg, "Apple")
    assert apple.markup_percent == 7
    assert apple.schema_offer == 69405880
    # by_currency
    assert resolve_fields(apple, 4, "USD")["fields[usd]"] == "{nominal} USD"
    # by_category приоритетнее by_currency
    assert resolve_fields(apple, 5, "USD")["fields[currency]"] == "TRY-SPECIAL"
    # валюта не из by_currency и нет общего fields → None (пропуск)
    assert resolve_fields(apple, 99, "AED") is None


# ───────────── orchestrator ─────────────

@pytest_asyncio.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.migrate.runner.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


class FakeAdmin:
    BASE = "https://funpay.com"

    def __init__(self):
        self._offers: list[int] = []
        self._next = 80000001
        self.saved: list[LotFields] = []

    async def get_lot_fields(self, lot_id, node_id=None):
        return LotFields(lot_id=0, node_id=node_id, raw_fields={"price": "", "amount": ""})

    async def save_lot(self, lot):
        self.saved.append(lot)
        self._offers.append(self._next)
        self._next += 1
        return {"ok": True}

    async def list_node_offers(self, node_id):
        return [{"offer_id": o, "title": "", "active": None} for o in self._offers]


class FakeStock:
    def __init__(self, categories):
        self.categories = categories


def _qty():
    return FieldType(key="quantity", type="int", name="Q", required=True)


def _steam_schema():
    return {
        "url": "x", "inputs": [
            {"name": "price", "type": "text", "value": ""},
            {"name": "fields[quantity]", "type": "text", "value": ""},
        ],
        "selects": [
            {"name": "fields[currency]", "options": [
                {"value": "USD", "text": "USD", "selected": False},
            ]},
            {"name": "fields[type]", "options": [
                {"value": "Подарочная карта", "text": "Подарочная карта", "selected": False},
            ]},
        ],
        "textareas": [{"name": "fields[desc][ru]", "value_preview": ""}],
    }


@pytest.mark.asyncio
async def test_migrate_platform_dry_run(tmp_path, db_factory):
    cfg = load_config(_write_config(tmp_path))
    steam = get_platform(cfg, "Steam")
    cats = [
        Category(category_id=2, category_name="Steam Wallet Code | USA",
                 services=[Service(service_id=2, service_name="Steam | USA | 50 USD",
                                   price=48.0, currency="USD", in_stock=100)],
                 fields=[_qty()]),
    ]
    admin = FakeAdmin()
    res = await migrate_platform(
        steam, admin=admin, schema=_steam_schema(), fx_rate=75.0,
        stock=FakeStock(cats), dry_run=True,
    )
    assert res.total_created == 0
    assert admin.saved == []


@pytest.mark.asyncio
async def test_migrate_platform_creates_and_skips(tmp_path, db_factory):
    cfg = load_config(_write_config(tmp_path))
    steam = get_platform(cfg, "Steam")
    cats = [
        # пригодная, в наличии → создать
        Category(category_id=2, category_name="Steam Wallet Code | USA",
                 services=[Service(service_id=2, service_name="Steam | USA | 50 USD",
                                   price=48.0, currency="USD", in_stock=100)],
                 fields=[_qty()]),
        # пустышка (0 в наличии) → пропустить
        Category(category_id=24, category_name="Steam Wallet Code | UK",
                 services=[Service(service_id=99, service_name="Steam | UK | 20 GBP",
                                   price=25.0, currency="GBP", in_stock=0)],
                 fields=[_qty()]),
    ]
    admin = FakeAdmin()
    res = await migrate_platform(
        steam, admin=admin, schema=_steam_schema(), fx_rate=75.0,
        stock=FakeStock(cats), dry_run=False,
    )
    assert res.total_created == 1
    # маппинг создан для svc 2, staged
    async with db_factory() as s:
        m = (await s.execute(select(Mapping).where(Mapping.ns_service_id == 2))).scalar_one()
        assert m.enabled is False
        assert m.funpay_lot_id in {o["offer_id"] for o in await admin.list_node_offers(1086)}
    # UK-пустышка (все услуги 0) пропущена как непригодная «нет в наличии»
    uk = next(o for o in res.outcomes if o.category_id == 24)
    assert uk.result is None
    assert "налич" in uk.skipped_reason


@pytest.mark.asyncio
async def test_migrate_platform_skips_unmapped_currency(tmp_path, db_factory):
    """Apple by_currency только USD; категория EUR без рецепта → пропуск."""
    cfg = load_config(_write_config(tmp_path))
    apple = get_platform(cfg, "Apple")
    cats = [
        Category(category_id=35, category_name="Apple Gift Card | BE",
                 services=[Service(service_id=220, service_name="Apple | BE | 50 EUR",
                                   price=59.0, currency="EUR", in_stock=10)],
                 fields=[_qty()]),
    ]
    admin = FakeAdmin()
    res = await migrate_platform(
        apple, admin=admin, schema=_steam_schema(), fx_rate=75.0,
        stock=FakeStock(cats), dry_run=False,
    )
    assert res.total_created == 0
    be = res.outcomes[0]
    assert be.result is None
    assert "рецепта" in be.skipped_reason


@pytest.mark.asyncio
async def test_min_stock_filters_low_services(tmp_path, db_factory):
    cfg = load_config(_write_config(tmp_path))
    steam = get_platform(cfg, "Steam")
    cats = [
        Category(category_id=2, category_name="Steam Wallet Code | USA",
                 services=[
                     Service(service_id=2, service_name="Steam 50 USD", price=48.0,
                             currency="USD", in_stock=100),
                     Service(service_id=3, service_name="Steam 100 USD", price=96.0,
                             currency="USD", in_stock=2),  # ниже min_stock=3
                 ],
                 fields=[_qty()]),
    ]
    admin = FakeAdmin()
    res = await migrate_platform(
        steam, admin=admin, schema=_steam_schema(), fx_rate=75.0,
        stock=FakeStock(cats), dry_run=False, min_stock=3,
    )
    # создан только svc 2 (stock 100), svc 3 (stock 2) отфильтрован
    assert res.total_created == 1
    created_ids = [c.ns_service_id for o in res.outcomes if o.result for c in o.result.created]
    assert created_ids == [2]
