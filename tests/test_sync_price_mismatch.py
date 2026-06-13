"""
P0-4: сигнал о расхождении цены FunPay vs last_synced_price.

Если на re-GET текущая цена лота существенно отличается от последней,
которую записал бот (last_synced_price), значит прошлый save_lot не
применился (или цену сменили вручную). sync_once считает такие лоты в
result["price_mismatches"] → _safe_sync шлёт WARNING (это сигнал, не
ошибка: цену sync выровняет на этом же цикле).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base, Mapping
from src.db.repo import upsert_mapping
from src.ns.models import Category, Service, StockResponse
import src.sync.stock_sync as ss


def _settings(**overrides) -> Settings:
    base = dict(
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        enable_real_actions=False,  # dry-run: реальный save_lot не нужен
        telegram_bot_token=None, telegram_use_proxy=False,
        funpay_currency="RUB",
        sync_max_price_change_percent=100000.0,
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


class _FP:
    """get_lot_fields отдаёт лот с настраиваемой текущей ценой."""
    def __init__(self, current_price: float):
        class _Lot:
            def __init__(self):
                self.lot_id = 1
                self.active = True
                self.amount = 50
                self.price = current_price
        self._lot = _Lot()

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None
    async def connect(self): return None
    async def get_lot_fields(self, lot_id, node_id=None): return self._lot
    async def save_lot(self, lot_fields): return {"ok": True}
    def get_and_reset_http_metrics(self):
        return {"ok": 1, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}


class _NS:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None
    async def get_stock(self):
        return StockResponse(categories=[
            Category(category_id=1, category_name="Apple", services=[
                Service(service_id=42, service_name="Apple 10 TRY",
                        price=2.0, currency="usd", in_stock=50),
            ]),
        ])


async def _set_last_synced_price(factory, lot_id: int, price: float) -> None:
    """Ставит last_synced_price, но НЕ last_synced_at — чтобы diff-cache
    промахнулся и sync реально сходил на FunPay (иначе cache-hit)."""
    async with factory() as s:
        m = (await s.execute(
            select(Mapping).where(Mapping.funpay_lot_id == lot_id)
        )).scalar_one()
        m.last_synced_price = price
        await s.commit()


async def test_price_mismatch_detected(db_factory, monkeypatch):
    settings = _settings()
    monkeypatch.setattr("src.sync.stock_sync.get_settings", lambda: settings)

    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=1, ns_service_id=42,
            markup_percent=10.0, stock_cap=100,
            ns_fields_template='{"q":"@QUANTITY"}',
            enabled=True, label="Apple 10 TRY",
        )
        await s.commit()
    # Бот «записал» 100₽, а FunPay показывает 200₽ — наш write не применился.
    await _set_last_synced_price(db_factory, 1, 100.0)

    result = await ss.sync_once(funpay_client=_FP(current_price=200.0), ns_client=_NS())
    assert result["price_mismatches"] == 1


async def test_no_mismatch_when_price_matches(db_factory, monkeypatch):
    settings = _settings()
    monkeypatch.setattr("src.sync.stock_sync.get_settings", lambda: settings)

    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=1, ns_service_id=42,
            markup_percent=10.0, stock_cap=100,
            ns_fields_template='{"q":"@QUANTITY"}',
            enabled=True, label="Apple 10 TRY",
        )
        await s.commit()
    # FunPay показывает ровно то, что записал бот → расхождения нет.
    await _set_last_synced_price(db_factory, 1, 165.0)

    result = await ss.sync_once(funpay_client=_FP(current_price=165.0), ns_client=_NS())
    assert result["price_mismatches"] == 0


async def test_no_mismatch_when_never_synced(db_factory, monkeypatch):
    """last_synced_price=None (лот ещё не синкали) → не сигналим."""
    settings = _settings()
    monkeypatch.setattr("src.sync.stock_sync.get_settings", lambda: settings)

    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=1, ns_service_id=42,
            markup_percent=10.0, stock_cap=100,
            ns_fields_template='{"q":"@QUANTITY"}',
            enabled=True, label="Apple 10 TRY",
        )
        await s.commit()

    result = await ss.sync_once(funpay_client=_FP(current_price=999.0), ns_client=_NS())
    assert result["price_mismatches"] == 0
