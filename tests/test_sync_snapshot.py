"""
P0-1 Фаза B (этап B3): snapshot-sync по нодам + детектор деградации.

Юнит: _snapshot_in_sync — решает, нужен ли per-lot offerEdit GET.
Интеграция sync_once:
  * snapshot OFF → старый per-lot путь (list_node_offers НЕ зовётся);
  * snapshot ON, лот в синке → per-lot GET ПРОПУЩЕН (snapshot_synced);
  * snapshot ON, лот разошёлся → fallthrough на per-lot GET;
  * деградация (snapshot-GET 429/fail ≥ порога) → degraded, ни одного
    per-lot GET/save (бережём rate-budget).
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base
from src.db.repo import upsert_mapping
from src.ns.models import Category, Service, StockResponse
import src.sync.stock_sync as ss


def _settings(**overrides) -> Settings:
    base = dict(
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        enable_real_actions=False,
        telegram_bot_token=None, telegram_use_proxy=False,
        funpay_currency="RUB",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.sync.stock_sync.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def patch_fx(monkeypatch):
    async def _fx(_settings=None):
        return 75.0
    monkeypatch.setattr(ss, "get_usd_rub_rate", _fx)


class _NS:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None

    async def get_stock(self):
        return StockResponse(categories=[
            Category(category_id=1, category_name="Apple", services=[
                Service(service_id=42, service_name="Apple 10 TRY",
                        price=0.5, currency="usd", in_stock=100),
            ]),
        ])


class _FakeLot:
    def __init__(self, lot_id, *, price=999.0, amount=100, active=True):
        self.lot_id = lot_id
        self.node_id = 1316
        self.price = price
        self.amount = amount
        self.active = active


class _FakeFP:
    def __init__(self, *, node_offers=None, raise_nodes=None):
        self.node_offers = node_offers or {}
        self.raise_nodes = raise_nodes or set()
        self.list_calls: list[int] = []
        self.get_lot_fields_calls: list[int] = []
        self.save_calls: list[int] = []

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None
    async def connect(self): return None

    async def list_node_offers(self, node_id):
        self.list_calls.append(node_id)
        if node_id in self.raise_nodes:
            raise RuntimeError("429 exhausted (snapshot)")
        return self.node_offers.get(node_id, [])

    async def get_lot_fields(self, lot_id, node_id=None):
        self.get_lot_fields_calls.append(lot_id)
        return _FakeLot(lot_id)

    async def save_lot(self, lot):
        self.save_calls.append(getattr(lot, "lot_id", None))
        return {"ok": True}

    def get_and_reset_http_metrics(self):
        return {"ok": 0, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}


# ───────────── юнит: _snapshot_in_sync ─────────────

class _Target:
    def __init__(self, stock: int, price: float):
        self.stock = stock
        self._price = price
    def round_price(self) -> float:
        return self._price


def _st():
    return _settings()


def test_snapshot_in_sync_active_match():
    s = _st()
    t = _Target(stock=10, price=100.0)
    snap = {"price": 100.0, "amount": 10}
    assert ss._snapshot_in_sync(t, snap, s) is True


def test_snapshot_in_sync_active_price_diff():
    s = _st()
    t = _Target(stock=10, price=100.0)
    # цена на FunPay сильно ниже target → нужно обновить
    assert ss._snapshot_in_sync(t, {"price": 50.0, "amount": 10}, s) is False


def test_snapshot_in_sync_active_amount_diff():
    s = _st()
    t = _Target(stock=10, price=100.0)
    assert ss._snapshot_in_sync(t, {"price": 100.0, "amount": 7}, s) is False


def test_snapshot_in_sync_active_missing_snap():
    s = _st()
    t = _Target(stock=10, price=100.0)
    assert ss._snapshot_in_sync(t, None, s) is False


def test_snapshot_in_sync_active_missing_price():
    s = _st()
    t = _Target(stock=10, price=100.0)
    assert ss._snapshot_in_sync(t, {"price": None, "amount": 10}, s) is False


def test_snapshot_in_sync_inactive_absent_is_synced():
    s = _st()
    t = _Target(stock=0, price=100.0)
    # target неактивен, оффера нет в активном листинге → уже снят → in-sync
    assert ss._snapshot_in_sync(t, None, s) is True


def test_snapshot_in_sync_inactive_present_needs_deactivation():
    s = _st()
    t = _Target(stock=0, price=100.0)
    assert ss._snapshot_in_sync(t, {"price": 100.0, "amount": 5}, s) is False


# ───────────── интеграция sync_once ─────────────

async def _seed(db_factory):
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            markup_percent=10.0, stock_cap=100,
            ns_fields_template='{"q":"@QUANTITY"}',
            enabled=True, label="Apple 10 TRY", funpay_node_id=1316,
        )
        await s.commit()


async def test_snapshot_off_uses_per_lot(db_factory, monkeypatch):
    settings = _settings(sync_snapshot_mode=False)
    monkeypatch.setattr(ss, "get_settings", lambda: settings)
    await _seed(db_factory)

    fp = _FakeFP()
    await ss.sync_once(funpay_client=fp, ns_client=_NS())

    assert fp.list_calls == []  # snapshot не трогали
    assert fp.get_lot_fields_calls == [100]  # per-lot путь


async def test_snapshot_in_sync_skips_per_lot_get(db_factory, monkeypatch):
    settings = _settings(sync_snapshot_mode=True)
    monkeypatch.setattr(ss, "get_settings", lambda: settings)
    monkeypatch.setattr(ss, "_snapshot_in_sync", lambda *a, **k: True)
    await _seed(db_factory)

    fp = _FakeFP(node_offers={1316: [{"offer_id": 100, "price": 999.0, "amount": 100}]})
    result = await ss.sync_once(funpay_client=fp, ns_client=_NS())

    assert fp.list_calls == [1316]  # snapshot ноды снят (1 GET)
    assert fp.get_lot_fields_calls == []  # per-lot GET ПРОПУЩЕН
    assert result["snapshot_synced"] == 1
    assert result["unchanged"] == 1
    assert result["degraded"] is False


async def test_snapshot_mismatch_falls_through_to_per_lot(db_factory, monkeypatch):
    settings = _settings(sync_snapshot_mode=True)
    monkeypatch.setattr(ss, "get_settings", lambda: settings)
    monkeypatch.setattr(ss, "_snapshot_in_sync", lambda *a, **k: False)
    await _seed(db_factory)

    fp = _FakeFP(node_offers={1316: [{"offer_id": 100, "price": 1.0, "amount": 1}]})
    await ss.sync_once(funpay_client=fp, ns_client=_NS())

    assert fp.list_calls == [1316]
    assert fp.get_lot_fields_calls == [100]  # разошлось → per-lot GET


async def test_snapshot_degradation_skips_cycle(db_factory, monkeypatch):
    # порог 1 → одна упавшая нода = деградация
    settings = _settings(
        sync_snapshot_mode=True, sync_snapshot_degraded_node_threshold=1
    )
    monkeypatch.setattr(ss, "get_settings", lambda: settings)
    await _seed(db_factory)

    fp = _FakeFP(raise_nodes={1316})
    result = await ss.sync_once(funpay_client=fp, ns_client=_NS())

    assert result["degraded"] is True
    assert result["snapshot_failed_nodes"] == 1
    assert fp.get_lot_fields_calls == []  # НИ одного per-lot GET
    assert fp.save_calls == []
    assert result["updated"] == 0
