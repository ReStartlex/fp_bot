"""
Тесты runtime-override для shop_markup_percent и shop_referral_percent.

Покрываем:
- get/set атомарны;
- невалидные значения отвергаются;
- fallback к settings.shop_markup_percent если override снят;
- snapshot отдаёт shop-overrides вместе с другими.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.config_runtime import (
    get_overrides_snapshot,
    get_shop_markup_percent,
    get_shop_referral_percent,
    set_shop_markup_percent,
    set_shop_referral_percent,
)
from src.db.models import Base


def _settings():
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x", ns_api_secret="QQ==",
        funpay_golden_key="x", funpay_user_id=1,
        shop_markup_percent=8.0, shop_referral_percent=1.0,
    )


@pytest.fixture()
async def db_setup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.config_runtime.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


async def test_shop_markup_default_from_env(db_setup):
    s = _settings()
    eff = await get_shop_markup_percent(s)
    assert eff == 8.0


async def test_shop_markup_override(db_setup):
    s = _settings()
    await set_shop_markup_percent(12.5)
    eff = await get_shop_markup_percent(s)
    assert eff == 12.5


async def test_shop_markup_reset_to_default(db_setup):
    s = _settings()
    await set_shop_markup_percent(20.0)
    await set_shop_markup_percent(None)
    eff = await get_shop_markup_percent(s)
    assert eff == 8.0  # снова из .env


async def test_shop_markup_validation(db_setup):
    with pytest.raises(ValueError):
        await set_shop_markup_percent(-1.0)
    with pytest.raises(ValueError):
        await set_shop_markup_percent(150.0)


async def test_shop_referral_default_from_env(db_setup):
    s = _settings()
    eff = await get_shop_referral_percent(s)
    assert eff == 1.0


async def test_shop_referral_override(db_setup):
    s = _settings()
    await set_shop_referral_percent(2.5)
    eff = await get_shop_referral_percent(s)
    assert eff == 2.5


async def test_overrides_snapshot_includes_shop(db_setup):
    await set_shop_markup_percent(10.0)
    snap = await get_overrides_snapshot()
    assert "shop_markup_percent" in snap
    assert snap["shop_markup_percent"] is not None
    assert "shop_referral_percent" in snap
    assert snap["shop_referral_percent"] is None  # не задан
