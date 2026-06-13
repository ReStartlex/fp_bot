"""
P0-3: watchdog протухания golden_key.

Покрываем ядро механизма:
  1. sync_once при FunPayAuthError из get_lot_fields НЕ падает, а
     помечает лот auth_error и возвращает result["auth_errors"] > 0
     (это сигнал, по которому _safe_sync шлёт алерт «обнови golden_key»).
  2. FunPayClient.check_auth возвращает False, когда whoami говорит
     authenticated=False (страница логина), True — когда авторизован,
     и True (не паникуем) при сетевой ошибке whoami.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base
from src.db.repo import upsert_mapping
from src.funpay.admin_http import FunPayAuthError
from src.funpay.client import FunPayClient
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


class _AuthFailFP:
    """FunPay-клиент с протухшим golden_key: get_lot_fields кидает auth."""
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None
    async def connect(self): return None

    async def get_lot_fields(self, lot_id: int, node_id=None):
        raise FunPayAuthError("FunPay перебросил на форму логина — обнови golden_key")

    async def save_lot(self, lot_fields):
        return {"ok": True}

    def get_and_reset_http_metrics(self):
        return {"ok": 0, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}


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


async def test_sync_once_surfaces_auth_errors(db_factory, monkeypatch):
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

    result = await ss.sync_once(funpay_client=_AuthFailFP(), ns_client=_NS())

    # sync не упал, лот пропущен, auth_errors просигналил
    assert result["auth_errors"] == 1
    assert result["skipped"] == 1
    assert result["updated"] == 0


# ───────────── check_auth ─────────────

class _FakeAdmin:
    def __init__(self, authed: bool | None, raise_exc: bool = False):
        self._authed = authed
        self._raise = raise_exc

    async def whoami(self):
        if self._raise:
            raise RuntimeError("network down")
        return {"authenticated": self._authed, "user_id": 617001 if self._authed else None}


def _client_with_admin(admin) -> FunPayClient:
    fp = FunPayClient(_settings())
    fp._admin_client_cache = admin  # property отдаёт кеш, реальный HTTP не трогаем
    return fp


@pytest.mark.asyncio
async def test_check_auth_true_when_authenticated():
    fp = _client_with_admin(_FakeAdmin(authed=True))
    assert await fp.check_auth() is True


@pytest.mark.asyncio
async def test_check_auth_false_when_login_page():
    fp = _client_with_admin(_FakeAdmin(authed=False))
    assert await fp.check_auth() is False


@pytest.mark.asyncio
async def test_check_auth_true_on_network_error():
    """Сетевой сбой whoami != «ключ протух» — не паникуем, возвращаем True."""
    fp = _client_with_admin(_FakeAdmin(authed=None, raise_exc=True))
    assert await fp.check_auth() is True
