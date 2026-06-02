"""
Тесты для отображения «capped» в логах sync_once.

Когда NS возвращает больше единиц товара, чем разрешает effective cap,
мы должны:
  1. Инкрементить счётчик lots_capped.
  2. Дописывать «(capped: NS=N>cap=K)» в action_str (видно через loguru).
  3. В Sync done показывать `capped=N` (только если N>0).

Это UX-диагностика: помогает оператору отличить «нет sync'а» от
«работает, но cap режет stock».
"""
from __future__ import annotations

import sys

import pytest
from loguru import logger
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
        enable_real_actions=False,  # dry-run
        telegram_bot_token=None,
        telegram_use_proxy=False,
        funpay_currency="RUB",
        # Отключаем guardrail на price change — нам важна логика cap'а,
        # а не защита от прайс-шока.
        sync_max_price_change_percent=10000.0,
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


@pytest.fixture()
def loguru_capture():
    """Снимает строки из loguru и возвращает join по \\n."""
    buf: list[str] = []
    sink_id = logger.add(lambda msg: buf.append(str(msg)), level="INFO")
    yield buf
    logger.remove(sink_id)


class _FakeFP:
    """Минимальный FunPayClient stub для dry-run прогонов sync_once."""
    def __init__(self, *, current_stock: int = 50, current_price: float = 30.0):
        class _Lot:
            def __init__(self):
                self.lot_id = 1
                self.active = True
                self.amount = current_stock
                self.price = current_price
        self._lot = _Lot()

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None
    async def connect(self): return None

    async def get_lot_fields(self, lot_id: int):
        return self._lot

    async def save_lot(self, lot_fields):
        return {"ok": True}

    def get_and_reset_http_metrics(self):
        return {"ok": 1, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}


def _make_ns(in_stock: int = 1500) -> "_FakeNS":
    return _FakeNS(in_stock=in_stock)


class _FakeNS:
    """Stub NS-клиента, возвращает Service с настраиваемым in_stock."""
    def __init__(self, in_stock: int = 1500):
        self.service = Service(
            service_id=42,
            service_name="Apple 10 TRY",
            price=0.5,
            currency="usd",
            in_stock=in_stock,
        )

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None

    async def get_stock(self):
        return StockResponse(categories=[
            Category(category_id=1, category_name="Apple", services=[self.service]),
        ])


@pytest.fixture(autouse=True)
def patch_fx(monkeypatch):
    async def _fx(_settings=None):
        return 73.69
    monkeypatch.setattr(ss, "get_usd_rub_rate", _fx)


async def test_capped_lot_increments_counter(db_factory, loguru_capture, monkeypatch):
    """NS_in_stock=1500, cap=100 → target=100, в логе action_str получает
    суффикс `(capped: NS=1500>cap=100)`, lots_capped == 1."""
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

    result = await ss.sync_once(
        funpay_client=_FakeFP(),
        ns_client=_make_ns(in_stock=1500),
    )

    assert result["capped"] == 1
    full_log = "\n".join(loguru_capture)
    assert "capped: NS=1500>cap=100" in full_log, full_log


async def test_not_capped_when_ns_stock_below_cap(db_factory, monkeypatch):
    """NS_in_stock=20, cap=100 → не capped, счётчик 0, никакого суффикса."""
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

    result = await ss.sync_once(
        funpay_client=_FakeFP(),
        ns_client=_make_ns(in_stock=20),
    )
    assert result["capped"] == 0


async def test_sync_done_omits_capped_when_zero(db_factory, loguru_capture, monkeypatch):
    """Если capped=0, в строке Sync done не должно быть `capped=0`."""
    settings = _settings()
    monkeypatch.setattr("src.sync.stock_sync.get_settings", lambda: settings)

    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=1, ns_service_id=42,
            markup_percent=10.0, stock_cap=100,
            ns_fields_template='{"q":"@QUANTITY"}',
            enabled=True, label="X",
        )
        await s.commit()

    await ss.sync_once(
        funpay_client=_FakeFP(),
        ns_client=_make_ns(in_stock=5),
    )

    sync_done = [line for line in loguru_capture if "Sync done" in line]
    assert sync_done, "Должна быть строка 'Sync done'"
    assert "capped=" not in sync_done[0], (
        f"При capped=0 не должно быть 'capped=' в строке: {sync_done[0]}"
    )


async def test_sync_done_includes_capped_when_positive(db_factory, loguru_capture, monkeypatch):
    """При capped>0 строка Sync done содержит `capped=N`."""
    settings = _settings()
    monkeypatch.setattr("src.sync.stock_sync.get_settings", lambda: settings)

    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=1, ns_service_id=42,
            markup_percent=10.0, stock_cap=100,
            ns_fields_template='{"q":"@QUANTITY"}',
            enabled=True, label="X",
        )
        await s.commit()

    await ss.sync_once(
        funpay_client=_FakeFP(),
        ns_client=_make_ns(in_stock=2000),
    )

    sync_done = [line for line in loguru_capture if "Sync done" in line]
    assert sync_done
    assert "capped=1" in sync_done[0], sync_done[0]
