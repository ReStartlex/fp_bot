"""
Тесты создания лотов и маппингов миграции (runner).

Фейковый admin симулирует FunPay: создание лота добавляет новый
offer_id в список офферов раздела (как реальный list_node_offers).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Mapping
from src.db.repo import upsert_mapping
from src.funpay.admin_http import LotFields
from src.migrate.loader import MigrationEntry, MigrationService
from src.migrate import runner as runner_mod
from src.migrate.runner import run_category


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
    """FunPay-заглушка: помнит офферы раздела, создание добавляет новый id."""
    BASE = "https://funpay.com"

    def __init__(self, *, start_offers: list[int] | None = None, fail_on=None):
        self._offers = list(start_offers or [])
        self._next_id = (max(self._offers) + 1) if self._offers else 70000001
        self.saved: list[LotFields] = []
        self._fail_on = fail_on or set()

    async def get_lot_fields(self, lot_id: int, node_id=None):
        # форма создания: пустой LotFields с node
        return LotFields(lot_id=0, node_id=node_id, raw_fields={"price": "", "amount": ""})

    async def save_lot(self, lot: LotFields):
        # имитируем валидацию: если в raw_fields маркер fail — ok=False
        nominal = lot.raw_fields.get("fields[usd]", "")
        if nominal in self._fail_on:
            return {"ok": False, "funpay_error": "forced fail"}
        self.saved.append(lot)
        # "создаём" оффер — появляется новый id
        self._offers.append(self._next_id)
        self._next_id += 1
        return {"ok": True}

    async def list_node_offers(self, node_id: int):
        return [{"offer_id": oid, "title": "", "active": None} for oid in self._offers]


def _entry(services) -> MigrationEntry:
    return MigrationEntry(
        ns_category_id=4, ns_category_name="Apple | USA", platform="Apple",
        currency="USD", region_code="USA", region_ru="США", region_en="USA",
        funpay_node=1316, markup_percent=10.0,
        funpay_fields={"fields[currency]": "USD", "fields[usd]": "{nominal} USD"},
        summary_ru="Карта {nominal} USD", summary_en="Card {nominal} USD",
        desc_ru="d", desc_en="d", services=services,
    )


def _schema() -> dict:
    return {
        "url": "x", "inputs": [{"name": "price", "type": "text", "value": ""}],
        "selects": [
            {"name": "fields[currency]", "options": [{"value": "USD", "text": "USD", "selected": True}]},
            {"name": "fields[usd]", "options": [
                {"value": "2 USD", "text": "2 USD", "selected": False},
                {"value": "5 USD", "text": "5 USD", "selected": False},
            ]},
        ],
        "textareas": [{"name": "fields[summary][ru]", "value_preview": ""}],
    }


@pytest.mark.asyncio
async def test_dry_run_creates_nothing(db_factory):
    entry = _entry([MigrationService(20, 2, 1.93, 100)])
    admin = FakeAdmin()
    res = await run_category(entry, admin, _schema(), 75.0, dry_run=True)
    assert res.created == []
    assert admin.saved == []


@pytest.mark.asyncio
async def test_creates_lot_and_mapping_staged(db_factory):
    entry = _entry([MigrationService(20, 2, 1.93, 100)])
    admin = FakeAdmin(start_offers=[69300023])
    res = await run_category(
        entry, admin, _schema(), 75.0, dry_run=False, activate=False,
        inter_request_delay_seconds=0,
    )
    assert len(res.created) == 1
    created = res.created[0]
    assert created.ns_service_id == 20
    # маппинг записан и staged (enabled=False)
    async with db_factory() as s:
        m = (await s.execute(
            select(Mapping).where(Mapping.ns_service_id == 20)
        )).scalar_one()
        assert m.funpay_lot_id == created.funpay_lot_id
        assert m.enabled is False
        assert m.ns_fields_template == runner_mod.NS_QUANTITY_TEMPLATE
        assert "Apple" in m.label and "2" in m.label
        # P1-4: KnownLot с непустым title создан сразу при создании лота
        from src.db.models import KnownLot
        kl = await s.get(KnownLot, created.funpay_lot_id)
        assert kl is not None
        assert kl.title and "2" in kl.title  # подставленный summary_ru
        assert kl.notified_at is not None  # помечен, чтобы discovery не шумел


@pytest.mark.asyncio
async def test_activate_makes_lot_active_and_mapping_enabled(db_factory):
    entry = _entry([MigrationService(20, 2, 1.93, 100)])
    admin = FakeAdmin()
    res = await run_category(
        entry, admin, _schema(), 75.0, dry_run=False, activate=True,
        inter_request_delay_seconds=0,
    )
    assert len(res.created) == 1
    assert admin.saved[0].active is True
    async with db_factory() as s:
        m = (await s.execute(
            select(Mapping).where(Mapping.ns_service_id == 20)
        )).scalar_one()
        assert m.enabled is True


@pytest.mark.asyncio
async def test_idempotent_skips_already_mapped(db_factory):
    # услуга 20 уже замаплена
    async with db_factory() as s:
        await upsert_mapping(s, funpay_lot_id=69300023, ns_service_id=20, enabled=True)
        await s.commit()

    entry = _entry([
        MigrationService(20, 2, 1.93, 100),  # уже есть → skip
        MigrationService(23, 5, 4.81, 200),  # новая → создать
    ])
    admin = FakeAdmin(start_offers=[69300023])
    res = await run_category(
        entry, admin, _schema(), 75.0, dry_run=False, inter_request_delay_seconds=0,
    )
    assert 20 in res.skipped_already_mapped
    assert [c.ns_service_id for c in res.created] == [23]
    assert len(admin.saved) == 1


@pytest.mark.asyncio
async def test_unsupported_nominal_not_created(db_factory):
    # номинал 99 нет в схеме → не создаётся
    entry = _entry([
        MigrationService(20, 2, 1.93, 100),    # ok
        MigrationService(99, 99, 90.0, 10),    # нет "99 USD" в селекте
    ])
    admin = FakeAdmin()
    res = await run_category(
        entry, admin, _schema(), 75.0, dry_run=False, inter_request_delay_seconds=0,
    )
    assert [c.ns_service_id for c in res.created] == [20]
    assert 99 in res.skipped_unsupported


@pytest.mark.asyncio
async def test_limit_respected(db_factory):
    entry = _entry([
        MigrationService(20, 2, 1.93, 100),
        MigrationService(23, 5, 4.81, 200),
    ])
    admin = FakeAdmin()
    res = await run_category(
        entry, admin, _schema(), 75.0, dry_run=False, limit=1,
        inter_request_delay_seconds=0,
    )
    assert len(res.created) == 1


@pytest.mark.asyncio
async def test_save_fail_recorded_as_error(db_factory):
    entry = _entry([MigrationService(20, 2, 1.93, 100)])
    admin = FakeAdmin(fail_on={"2 USD"})
    res = await run_category(
        entry, admin, _schema(), 75.0, dry_run=False, inter_request_delay_seconds=0,
    )
    assert res.created == []
    assert len(res.errors) == 1
    # маппинг НЕ создан при провале
    async with db_factory() as s:
        rows = (await s.execute(select(Mapping))).scalars().all()
        assert rows == []
