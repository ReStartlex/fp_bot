"""
Тесты discovery новых FunPay-лотов.

Проверяем что:
- Первый запуск помечает все лоты «известными» и шлёт уведомления для
  всех тех, у которых нет маппинга.
- Повторный запуск тех же лотов нотификаций не плодит.
- Лот, имеющий маппинг, не нотифицируется (но last_seen_at обновляется).
- Если FunPay недоступен — функция возвращает нули, исключение не пробрасывает.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, KnownLot
from src.db.repo import upsert_mapping
from src.ns.models import Category, Service, StockResponse
from src.sync.new_lots import discover_new_lots


@dataclass
class FakeLot:
    id: int
    description: str = ""


class FakeFunPay:
    def __init__(self, lots, *, raise_on_get: bool = False):
        self._lots = lots
        self._raise = raise_on_get

    async def get_my_lots(self):
        if self._raise:
            raise RuntimeError("FunPay недоступен")
        return list(self._lots)


class FakeTG:
    def __init__(self):
        self.calls: list[tuple[int, str | None, list]] = []

    async def new_lot_discovered(self, lot_id, title, *, suggestions=None):
        self.calls.append((lot_id, title, list(suggestions or [])))


class FakeNS:
    async def get_stock(self):
        return StockResponse(categories=[
            Category(
                category_id=1,
                category_name="Steam",
                services=[
                    Service(
                        service_id=300,
                        service_name="Steam Gift Card | US | 5 USD",
                        price=4.8,
                        currency="USD",
                        in_stock=10,
                    )
                ],
            )
        ])


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.sync.new_lots.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_first_run_notifies_unmapped_lots(db_factory):
    fp = FakeFunPay([
        FakeLot(id=100, description="Apple 2 USD"),
        FakeLot(id=200, description="Steam 5 USD"),
    ])
    tg = FakeTG()
    stats = await discover_new_lots(fp, tg)
    assert stats == {"seen": 2, "new": 2, "notified": 2}
    notified_ids = sorted(c[0] for c in tg.calls)
    assert notified_ids == [100, 200]

    # КnownLot теперь содержит обе позиции
    async with db_factory() as s:
        rows = (await s.execute(select(KnownLot))).scalars().all()
    assert sorted(r.funpay_lot_id for r in rows) == [100, 200]
    assert all(r.notified_at is not None for r in rows)


@pytest.mark.asyncio
async def test_second_run_does_not_notify_again(db_factory):
    fp = FakeFunPay([FakeLot(id=100, description="Apple")])
    tg = FakeTG()
    await discover_new_lots(fp, tg)
    tg.calls.clear()

    # Второй прогон — те же лоты, нотификаций быть не должно
    stats = await discover_new_lots(fp, tg)
    assert stats["new"] == 0
    assert stats["notified"] == 0
    assert tg.calls == []


@pytest.mark.asyncio
async def test_mapped_lot_does_not_notify(db_factory):
    # Заранее создаём маппинг для лота 100
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=20, label="Apple",
        )
        await s.commit()

    fp = FakeFunPay([
        FakeLot(id=100, description="Apple"),       # уже мапнут
        FakeLot(id=200, description="Steam"),       # новый, нужен пуш
    ])
    tg = FakeTG()
    stats = await discover_new_lots(fp, tg)
    assert stats == {"seen": 2, "new": 1, "notified": 1}
    assert [(lot_id, title) for lot_id, title, _ in tg.calls] == [(200, "Steam")]


@pytest.mark.asyncio
async def test_funpay_failure_swallowed(db_factory):
    fp = FakeFunPay([], raise_on_get=True)
    tg = FakeTG()
    stats = await discover_new_lots(fp, tg)
    assert stats == {"seen": 0, "new": 0, "notified": 0}
    assert tg.calls == []


@pytest.mark.asyncio
async def test_missing_funpay_returns_zero(db_factory):
    tg = FakeTG()
    stats = await discover_new_lots(None, tg)
    assert stats == {"seen": 0, "new": 0, "notified": 0}


@pytest.mark.asyncio
async def test_invalid_lot_ids_skipped(db_factory):
    fp = FakeFunPay([
        FakeLot(id=0, description="zero"),
        FakeLot(id=-1, description="neg"),
        FakeLot(id=42, description="ok"),
    ])
    tg = FakeTG()
    stats = await discover_new_lots(fp, tg)
    assert stats == {"seen": 1, "new": 1, "notified": 1}
    assert [(lot_id, title) for lot_id, title, _ in tg.calls] == [(42, "ok")]


@pytest.mark.asyncio
async def test_new_lot_notification_receives_ns_suggestions(db_factory):
    fp = FakeFunPay([FakeLot(id=200, description="Steam Gift Card 5 USD")])
    tg = FakeTG()

    settings = SimpleNamespace(
        new_lots_suggest_enabled=True,
        new_lots_suggest_max=3,
        new_lots_suggest_min_score=20,
    )
    stats = await discover_new_lots(fp, tg, ns_client=FakeNS(), settings=settings)

    assert stats == {"seen": 1, "new": 1, "notified": 1}
    assert tg.calls[0][2]
    assert tg.calls[0][2][0].service_id == 300
