"""
Тесты runtime-настроек (overrides поверх .env).

Главное свойство: если в БД лежит override — он берётся вместо .env;
если нет — fallback к .env. Сброс override (`set_x(None)`) убирает строку
и возвращает поведение к дефолту.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base
import src.config_runtime as rt


def _settings(*, markup: float = 6.0, premium: float = 2.0, stock_cap: int = 100) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        markup_percent=markup,
        usd_rub_premium_percent=premium,
        funpay_stock_cap=stock_cap,
    )


@pytest.fixture()
async def isolated_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(rt, "session_factory", lambda: factory)
    yield
    await engine.dispose()


# ─────────── markup ───────────

@pytest.mark.asyncio
async def test_markup_falls_back_to_env_when_no_override(isolated_db):
    s = _settings(markup=7.5)
    assert await rt.get_global_markup_percent(s) == pytest.approx(7.5)


@pytest.mark.asyncio
async def test_markup_override_takes_precedence(isolated_db):
    s = _settings(markup=6.0)
    await rt.set_global_markup_percent(4.2)
    assert await rt.get_global_markup_percent(s) == pytest.approx(4.2)


@pytest.mark.asyncio
async def test_markup_override_reset(isolated_db):
    s = _settings(markup=6.0)
    await rt.set_global_markup_percent(4.2)
    await rt.set_global_markup_percent(None)
    assert await rt.get_global_markup_percent(s) == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_markup_rejects_negative(isolated_db):
    with pytest.raises(ValueError):
        await rt.set_global_markup_percent(-1.0)


@pytest.mark.asyncio
async def test_markup_rejects_too_high(isolated_db):
    with pytest.raises(ValueError):
        await rt.set_global_markup_percent(500.0)


# ─────────── premium ───────────

@pytest.mark.asyncio
async def test_premium_override(isolated_db):
    s = _settings(premium=2.0)
    assert await rt.get_premium_percent(s) == pytest.approx(2.0)
    await rt.set_premium_percent(3.5)
    assert await rt.get_premium_percent(s) == pytest.approx(3.5)


@pytest.mark.asyncio
async def test_premium_validates_range(isolated_db):
    with pytest.raises(ValueError):
        await rt.set_premium_percent(99.0)


# ─────────── stock cap ───────────

@pytest.mark.asyncio
async def test_stock_cap_override(isolated_db):
    s = _settings(stock_cap=100)
    assert await rt.get_stock_cap(s) == 100
    await rt.set_stock_cap(25)
    assert await rt.get_stock_cap(s) == 25


@pytest.mark.asyncio
async def test_stock_cap_reset(isolated_db):
    s = _settings(stock_cap=100)
    await rt.set_stock_cap(25)
    await rt.set_stock_cap(None)
    assert await rt.get_stock_cap(s) == 100


@pytest.mark.asyncio
async def test_overrides_snapshot(isolated_db):
    snap = await rt.get_overrides_snapshot()
    assert snap == {
        rt.KEY_MARKUP: None,
        rt.KEY_PREMIUM: None,
        rt.KEY_STOCK_CAP: None,
    }
    await rt.set_global_markup_percent(5.0)
    snap = await rt.get_overrides_snapshot()
    assert snap[rt.KEY_MARKUP] is not None
    assert float(snap[rt.KEY_MARKUP]) == pytest.approx(5.0)
    assert snap[rt.KEY_PREMIUM] is None
