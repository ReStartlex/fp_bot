"""
Тесты для zombie-reaper'а — фоновой задачи устранения half-disabled state.

Сценарий: _emergency_disable_lot отключил mapping в БД, но save_lot на
FunPay не прошёл (например, из-за 429 burst). Reaper находит такие
лоты и пытается deactivate их снова.

Гарантии, которые проверяем:
  1. Идемпотентность: если лот уже active=False & amount=0 — save_lot
     НЕ вызывается, считается already_dead.
  2. Корректная reap: active=True или amount>0 → save_lot(False, 0).
  3. Rate-limit safety: max_per_run ограничивает за один прогон.
  4. Dry-run режим: enable_real_actions=False — save_lot НЕ зовётся,
     но в логике засчитывается «бы deactivated».
  5. Resilience: FunPay GET fail на одном лоте не ломает прогон.
  6. Уведомление owner'а при успешной reap.
  7. enabled=True mappings игнорируются (это не наша забота).
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base
from src.db.repo import upsert_mapping
from src.sync.zombie_reaper import (
    ReaperResult,
    _is_lot_already_dead,
    _set_lot_dead,
    reap_zombie_lots_once,
)


def _settings(**overrides) -> Settings:
    base = dict(
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x", funpay_user_id=1,
        enable_real_actions=True,
        telegram_bot_token=None,
        telegram_use_proxy=False,
        funpay_currency="RUB",
        zombie_lot_reaper_max_per_run=5,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.sync.zombie_reaper.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


class _FakeLot:
    """Минимальный stub FunPay lot_fields."""
    def __init__(self, lot_id: int, *, active: bool = True, amount: int = 99):
        self.lot_id = lot_id
        self.active = active
        self.amount = amount
        self.price = 100.0


class _FakeFP:
    """Stub FunPayClient — для каждого lot_id возвращает _FakeLot из словаря."""
    def __init__(self, lots: dict[int, _FakeLot] | None = None,
                 fail_on_get: set[int] | None = None,
                 fail_on_save: set[int] | None = None,
                 save_returns_ok_false: set[int] | None = None):
        self.lots = lots or {}
        self.fail_on_get = fail_on_get or set()
        self.fail_on_save = fail_on_save or set()
        self.save_returns_ok_false = save_returns_ok_false or set()
        self.save_calls: list[tuple[int, bool, int]] = []

    async def get_lot_fields(self, lot_id: int):
        if lot_id in self.fail_on_get:
            raise RuntimeError(f"FunPay GET {lot_id} failed")
        if lot_id not in self.lots:
            self.lots[lot_id] = _FakeLot(lot_id, active=True, amount=99)
        return self.lots[lot_id]

    async def save_lot(self, lot_fields):
        lot_id = getattr(lot_fields, "lot_id", -1)
        if lot_id in self.fail_on_save:
            raise RuntimeError(f"FunPay save_lot {lot_id} failed")
        if lot_id in self.save_returns_ok_false:
            return {"ok": False, "funpay_error": "rate-limited"}
        self.save_calls.append(
            (lot_id, lot_fields.active, lot_fields.amount)
        )
        return {"ok": True}


# ───────────────── Хелперы _is_lot_already_dead / _set_lot_dead ─────────────────

def test_is_lot_already_dead_both_conditions():
    """active=False и amount=0 → dead."""
    lot = _FakeLot(1, active=False, amount=0)
    assert _is_lot_already_dead(lot) is True


def test_is_lot_already_dead_active_true():
    """active=True → НЕ dead, даже если amount=0."""
    lot = _FakeLot(1, active=True, amount=0)
    assert _is_lot_already_dead(lot) is False


def test_is_lot_already_dead_amount_positive():
    """amount>0 → НЕ dead, даже если active=False (надо ещё раз save_lot)."""
    lot = _FakeLot(1, active=False, amount=99)
    assert _is_lot_already_dead(lot) is False


def test_set_lot_dead_writes_attrs():
    lot = _FakeLot(1, active=True, amount=99)
    _set_lot_dead(lot)
    assert lot.active is False
    assert lot.amount == 0


# ───────────────── Reaper integration tests ─────────────────

async def test_reap_skips_when_no_disabled_mappings(db_factory):
    """Нет disabled-маппингов — нечего reap'ить."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=1, ns_service_id=42,
            enabled=True,  # enabled, не должен попасть в reaper
        )
        await s.commit()

    fp = _FakeFP()
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)
    assert result.checked == 0
    assert result.deactivated == 0
    assert fp.save_calls == []


async def test_reap_deactivates_zombie_lot(db_factory):
    """disabled mapping + FunPay лот active=True → save_lot(False, 0)."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="Apple TR 10 TRY",
        )
        await s.commit()

    fp = _FakeFP({100: _FakeLot(100, active=True, amount=99)})
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)

    assert result.checked == 1
    assert result.deactivated == 1
    assert result.already_dead == 0
    assert result.errors == 0
    assert fp.save_calls == [(100, False, 0)]


async def test_reap_idempotent_already_dead(db_factory):
    """disabled mapping + FunPay лот active=False, amount=0 → НЕ save_lot."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="dead lot",
        )
        await s.commit()

    fp = _FakeFP({100: _FakeLot(100, active=False, amount=0)})
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)

    assert result.checked == 1
    assert result.already_dead == 1
    assert result.deactivated == 0
    assert fp.save_calls == []


async def test_reap_idempotent_active_false_but_amount_positive(db_factory):
    """active=False но amount=99 → НЕ dead, надо deactivate."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="half-dead",
        )
        await s.commit()

    fp = _FakeFP({100: _FakeLot(100, active=False, amount=99)})
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)

    assert result.deactivated == 1
    assert fp.save_calls == [(100, False, 0)]


async def test_reap_respects_max_per_run(db_factory):
    """Если зомби 10, max_per_run=3 → берём 3 за прогон."""
    settings = _settings(zombie_lot_reaper_max_per_run=3)
    async with db_factory() as s:
        for i in range(1, 11):
            await upsert_mapping(
                s, funpay_lot_id=1000 + i, ns_service_id=i,
                enabled=False, label=f"zombie{i}",
            )
        await s.commit()

    fp = _FakeFP({
        1000 + i: _FakeLot(1000 + i, active=True, amount=50)
        for i in range(1, 11)
    })
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)

    assert result.checked == 3
    assert result.deactivated == 3
    assert len(fp.save_calls) == 3


async def test_reap_dry_run_does_not_call_save_lot(db_factory):
    """enable_real_actions=False → deactivated++ но save_lot НЕ зовётся."""
    settings = _settings(enable_real_actions=False)
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="dry-run target",
        )
        await s.commit()

    fp = _FakeFP({100: _FakeLot(100, active=True, amount=99)})
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)

    assert result.deactivated == 1
    assert fp.save_calls == [], (
        "В dry-run save_lot НЕ должен зваться (а то реально deactivate'нём)"
    )


async def test_reap_get_failure_counts_as_error(db_factory):
    """FunPay GET упал на одном лоте — error++, остальные продолжают обрабатываться."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="bad lot",
        )
        await upsert_mapping(
            s, funpay_lot_id=101, ns_service_id=43,
            enabled=False, label="good lot",
        )
        await s.commit()

    fp = _FakeFP(
        lots={101: _FakeLot(101, active=True, amount=10)},
        fail_on_get={100},
    )
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)

    assert result.errors == 1
    assert result.deactivated == 1
    assert fp.save_calls == [(101, False, 0)]


async def test_reap_save_failure_counts_as_error(db_factory):
    """FunPay save_lot упал — error++, deactivated НЕ инкрементится."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="save will fail",
        )
        await s.commit()

    fp = _FakeFP(
        lots={100: _FakeLot(100, active=True, amount=99)},
        fail_on_save={100},
    )
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)

    assert result.errors == 1
    assert result.deactivated == 0


async def test_reap_save_returns_ok_false_counts_as_error(db_factory):
    """save_lot вернул {ok: False} — error++, не считаем deactivated."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="ok=false",
        )
        await s.commit()

    fp = _FakeFP(
        lots={100: _FakeLot(100, active=True, amount=99)},
        save_returns_ok_false={100},
    )
    result = await reap_zombie_lots_once(funpay_client=fp, settings=settings)
    assert result.errors == 1
    assert result.deactivated == 0


async def test_reap_calls_notify_owner_on_success(db_factory):
    """При успешной reap зовём notify_owner с описанием reaped лотов."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="zombie",
        )
        await s.commit()

    fp = _FakeFP({100: _FakeLot(100, active=True, amount=99)})
    notifications: list[str] = []

    async def notify(text: str) -> None:
        notifications.append(text)

    result = await reap_zombie_lots_once(
        funpay_client=fp, settings=settings, notify_owner=notify,
    )
    assert result.deactivated == 1
    assert len(notifications) == 1
    assert "100" in notifications[0]
    assert "zombie" in notifications[0]


async def test_reap_no_notification_when_no_deactivation(db_factory):
    """Если ничего не reap'нули — notify_owner НЕ зовём."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="already dead",
        )
        await s.commit()

    fp = _FakeFP({100: _FakeLot(100, active=False, amount=0)})
    notifications: list[str] = []

    async def notify(text: str) -> None:
        notifications.append(text)

    result = await reap_zombie_lots_once(
        funpay_client=fp, settings=settings, notify_owner=notify,
    )
    assert result.deactivated == 0
    assert notifications == []


async def test_reap_notify_failure_does_not_break(db_factory):
    """notify_owner упал — reaper всё равно возвращает result."""
    settings = _settings()
    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=100, ns_service_id=42,
            enabled=False, label="z",
        )
        await s.commit()

    fp = _FakeFP({100: _FakeLot(100, active=True, amount=10)})

    async def bad_notify(text: str) -> None:
        raise RuntimeError("Telegram down")

    result = await reap_zombie_lots_once(
        funpay_client=fp, settings=settings, notify_owner=bad_notify,
    )
    # Сам reap прошёл успешно — notify-провал не должен этого менять.
    assert result.deactivated == 1
    assert result.errors == 0


def test_reaper_result_reaped_property():
    """Свойство reaped: True если есть deactivated."""
    assert ReaperResult(deactivated=1).reaped is True
    assert ReaperResult(deactivated=0, already_dead=5).reaped is False
    assert ReaperResult(errors=3).reaped is False
