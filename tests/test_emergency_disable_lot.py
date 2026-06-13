"""
Аудит #8: _emergency_disable_lot должен отключать mapping в БД
ДАЖЕ если save_lot в FunPay упал.

Контракт fail-safe: после ошибки выдачи лот должен прекратить продаваться.
Если save_lot на FunPay упал (сеть, 5xx, что угодно), но mapping в БД
остался enabled — sync_stock на следующем цикле увидит «enabled,
с pricing» и включит лот обратно в продажу. Покупатели продолжат
покупать проблемный товар → новые failed-заказы.

Фикс: отключение mapping в БД делаем независимо от исхода save_lot.
Save_lot и БД-update — две независимые попытки, обе обязательны.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Mapping
from src.db.repo import upsert_mapping
from src.orders import processor as proc


@pytest_asyncio.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.orders.processor.session_factory", lambda: factory)
    monkeypatch.setattr("src.orders.stages.resolve.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


class FakeTG:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    async def error(self, text: str): self.errors.append(text)
    async def warning(self, text: str): self.warnings.append(text)


class FakeFunPaySaveFails:
    """save_lot ВСЕГДА падает."""

    async def get_lot_fields(self, lot_id: int):
        class Lot:
            def __init__(self, lot_id: int):
                self.lot_id = lot_id
                self.active = True
                self.amount = 100
        return Lot(lot_id)

    async def save_lot(self, lot_fields):
        raise RuntimeError("FunPay save_lot 500 server error")


class FakeFunPaySaveOK:
    """save_lot успешно отключает.

    Stateful: get_lot_fields возвращает один и тот же объект, чтобы
    verify-after-save в _emergency_disable_lot видел применённое
    изменение (active=False).
    """

    def __init__(self):
        self.saved_lots = []
        self._lots: dict[int, object] = {}

    async def get_lot_fields(self, lot_id: int):
        if lot_id not in self._lots:
            class Lot:
                def __init__(self, lot_id: int):
                    self.lot_id = lot_id
                    self.active = True
                    self.amount = 100
            self._lots[lot_id] = Lot(lot_id)
        return self._lots[lot_id]

    async def save_lot(self, lot_fields):
        self.saved_lots.append(lot_fields)
        return {"ok": True}


class FakeFunPaySaveSilentlyIgnored(FakeFunPaySaveOK):
    """Реальное поведение FunPay из инцидента 2026-06: save отвечает
    «успехом», но форму не применяет — лот остаётся active=True."""

    async def save_lot(self, lot_fields):
        self.saved_lots.append(lot_fields)
        lot_fields.active = True  # FunPay «откатил» деактивацию
        return {"ok": True}


async def _make_enabled_mapping(factory, lot_id: int = 7777) -> None:
    async with factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=lot_id, ns_service_id=10,
            markup_percent=5.0, stock_cap=10, enabled=True,
            label="Test",
        )
        await s.commit()


async def _get_mapping(factory, lot_id: int) -> Mapping | None:
    async with factory() as s:
        return (
            await s.execute(select(Mapping).where(Mapping.funpay_lot_id == lot_id))
        ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_disables_mapping_in_db_even_when_funpay_save_lot_fails(db_factory):
    """Аудит #8: save_lot упал → mapping в БД ВСЁ РАВНО должен стать
    enabled=False, иначе sync_stock на следующем цикле снова включит лот."""
    await _make_enabled_mapping(db_factory, lot_id=7777)
    fp = FakeFunPaySaveFails()
    tg = FakeTG()

    ok = await proc._emergency_disable_lot(
        7777, fp, tg, reason="ns_create_order упал", log=proc.logger,
    )

    assert ok is False, "save_lot упал, поэтому возврат — False"
    mapping = await _get_mapping(db_factory, 7777)
    assert mapping is not None
    assert mapping.enabled is False, (
        "Аудит #8: при fail save_lot mapping в БД ОБЯЗАН быть disabled, "
        "иначе sync_stock возродит лот."
    )


@pytest.mark.asyncio
async def test_disables_both_funpay_and_db_on_happy_path(db_factory):
    """Контроль: при успешном save_lot и mapping тоже отключается."""
    await _make_enabled_mapping(db_factory, lot_id=8888)
    fp = FakeFunPaySaveOK()
    tg = FakeTG()

    ok = await proc._emergency_disable_lot(
        8888, fp, tg, reason="test", log=proc.logger,
    )

    assert ok is True
    assert len(fp.saved_lots) == 1
    saved = fp.saved_lots[0]
    assert saved.active is False
    assert saved.amount == 100, (
        "amount НЕ должен обнуляться: FunPay отбраковывает форму с "
        "amount=0, деактивация = только снять active."
    )
    mapping = await _get_mapping(db_factory, 8888)
    assert mapping is not None
    assert mapping.enabled is False


@pytest.mark.asyncio
async def test_save_reported_ok_but_lot_still_active_is_failure(db_factory):
    """Инцидент 2026-06: FunPay отвечает «успехом», не применив форму.
    Verify-after-save должен поймать это: возврат False (алерт оператору),
    mapping в БД всё равно disabled."""
    await _make_enabled_mapping(db_factory, lot_id=9999)
    fp = FakeFunPaySaveSilentlyIgnored()
    tg = FakeTG()

    ok = await proc._emergency_disable_lot(
        9999, fp, tg, reason="NS вернул возврат/отмену: Refunded",
        log=proc.logger,
    )

    assert ok is False, (
        "Лот на FunPay остался активным — нельзя рапортовать успех."
    )
    assert len(tg.errors) == 1, "Оператор должен получить 🚨-алерт"
    mapping = await _get_mapping(db_factory, 9999)
    assert mapping is not None
    assert mapping.enabled is False
