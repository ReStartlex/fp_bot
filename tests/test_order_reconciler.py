from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Order
from src.orders import reconciler


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(reconciler, "session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_replays_stale_pins_ready_orders(db_factory, monkeypatch):
    async with db_factory() as session:
        session.add(
            Order(
                funpay_order_id="fp-stale",
                funpay_lot_id=100,
                ns_service_id=20,
                buyer_username="buyer",
                chat_id=555,
                quantity=1,
                status="pins_ready",
                updated_at=datetime.utcnow() - timedelta(minutes=5),
            )
        )
        await session.commit()

    seen: list[str] = []

    async def fake_process(event, **_kwargs):
        seen.append(event.funpay_order_id)
        return {"status": "delivered"}

    monkeypatch.setattr(reconciler, "process_funpay_order", fake_process)

    settings = type(
        "Settings",
        (),
        {
            "order_reconcile_enabled": True,
            "order_reconcile_stale_after_seconds": 60,
            "order_reconcile_max_per_run": 10,
        },
    )()

    result = await reconciler.reconcile_orders_once(settings=settings)

    assert seen == ["fp-stale"]
    assert result == {"checked": 1, "recovered": 1, "skipped": 0, "failed": 0}
