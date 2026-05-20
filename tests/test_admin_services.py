from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base, Mapping, Order, SyncRun
from src.services import admin


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1,
        ns_login="x",
        ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x",
        funpay_user_id=1,
        web_api_enabled=True,
    )


@pytest.mark.asyncio
async def test_dashboard_summary_counts_orders_mappings_and_sync(db_factory):
    async with db_factory() as session:
        session.add_all([
            Mapping(funpay_lot_id=100, ns_service_id=10, enabled=True),
            Mapping(funpay_lot_id=200, ns_service_id=20, enabled=False),
            Order(
                funpay_order_id="delivered",
                funpay_lot_id=100,
                ns_service_id=10,
                status="delivered",
            ),
            Order(
                funpay_order_id="pins",
                funpay_lot_id=100,
                ns_service_id=10,
                status="pins_ready",
            ),
            SyncRun(status="completed", lots_checked=2, lots_updated=1, lots_skipped=1),
        ])
        await session.commit()

    summary = await admin.get_dashboard_summary(_settings())

    assert summary["orders"]["total"] == 2
    assert summary["orders"]["active"] == 1
    assert summary["orders"]["problem"] == 1
    assert summary["mappings"] == {"enabled": 1, "disabled": 1, "total": 2}
    assert summary["sync"]["last_status"] == "completed"


@pytest.mark.asyncio
async def test_problem_items_include_failed_orders_and_disabled_mappings(db_factory):
    async with db_factory() as session:
        session.add_all([
            Mapping(funpay_lot_id=200, ns_service_id=20, enabled=False, label="Apple 5 USD"),
            Order(
                funpay_order_id="failed",
                funpay_lot_id=100,
                ns_service_id=10,
                status="failed",
                error="boom",
            ),
        ])
        await session.commit()

    problems = await admin.list_problem_items()

    assert problems["orders"][0]["funpay_order_id"] == "failed"
    assert problems["disabled_mappings"][0]["funpay_lot_id"] == 200
