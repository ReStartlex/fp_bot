"""
Аудит #3: двухфазная доставка pins.

Сценарий до фикса:
  1) status=pins_ready, send_message OK
  2) crash до commit'a status=delivered
  3) reconciler видит pins_ready → повторно зовёт processor → ДУБЛЬ pins в чат.

Фикс:
  1) ДО send_message: status=delivering, commit
  2) send_message
     - exception → откат status=pins_ready (сообщение НЕ ушло, retry безопасен)
     - success → status=delivered, commit
  3) crash между send_message OK и commit delivered:
     - статус остаётся delivering
     - reconciler видит delivering → НЕ повторяет автоматически,
       переводит в manual_hold + alert "проверьте чат вручную"

Это исключает двойную выдачу полностью: повтор возможен только если
send_message НЕ выполнялся (откат на pins_ready).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base, Order
from src.db.repo import find_order_by_funpay_id, upsert_mapping
from src.orders import processor as proc
from src.orders import reconciler as recon
from src.orders.processor import FunPayOrderEvent


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.orders.processor.session_factory", lambda: factory)
    monkeypatch.setattr("src.orders.reconciler.session_factory", lambda: factory)
    monkeypatch.setattr("src.db.repo.session_factory", lambda: factory, raising=False)
    proc._order_locks.clear()
    yield factory
    await engine.dispose()


def _settings(**overrides) -> Settings:
    base: dict = dict(
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        enable_real_actions=True,
        order_delivery_hard_timeout_seconds=600,
        telegram_bot_token=None,
        telegram_use_proxy=False,
        telegram_proxy_host=None,
        telegram_proxy_port=None,
        telegram_proxy_username=None,
        telegram_proxy_password=None,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


@pytest.fixture()
def settings() -> Settings:
    return _settings()


class FakeFunPay:
    def __init__(self, *, send_raises: BaseException | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.account = self
        self.saved_lots: list[Any] = []
        self.disabled_lots: list[int] = []
        self._send_raises = send_raises
        self.status_during_send: list[str] = []
        self._db_factory: Any = None

    async def send_message(self, chat_id: int, text: str):
        if self._db_factory is not None:
            async with self._db_factory() as s:
                o = await find_order_by_funpay_id(s, "fp-twophase")
                if o is not None:
                    self.status_during_send.append(o.status)
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append((chat_id, text))

    async def get_lot_fields(self, lot_id: int):
        class Lot:
            def __init__(self, lot_id: int):
                self.lot_id = lot_id
                self.active = True
                self.amount = 100
        return Lot(lot_id)

    async def save_lot(self, lot_fields):
        self.saved_lots.append(lot_fields)
        if getattr(lot_fields, "active", True) is False:
            self.disabled_lots.append(lot_fields.lot_id)
        return {"ok": True}


class FakeTelegram:
    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.successes: list[dict] = []
        self.failures: list[dict] = []
        self.manual_holds: list[dict] = []

    async def warning(self, text: str): self.warnings.append(text)
    async def error(self, text: str): self.errors.append(text)
    async def order_success(self, **kw): self.successes.append(kw)
    async def order_failure(self, **kw): self.failures.append(kw)
    async def manual_hold_required(self, **kw): self.manual_holds.append(kw)


def _event() -> FunPayOrderEvent:
    return FunPayOrderEvent(
        funpay_order_id="fp-twophase",
        funpay_lot_id=1001,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=1,
        funpay_price_rub=200.0,
        description="Apple Gift Card 2 USD",
    )


async def _make_mapping(factory) -> None:
    async with factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=1001, ns_service_id=20,
            markup_percent=5.0, stock_cap=10,
            ns_fields_template='{"quantity":"@QUANTITY"}',
            enabled=True, label="Apple USA 2",
        )
        await s.commit()


async def _seed_pins_ready_order(factory) -> None:
    async with factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-twophase",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id="ns-1",
                ns_price_usd=1.5,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="pins_ready",
                pins_json=json.dumps(["KEY-AAA"]),
            )
        )
        await s.commit()


async def _coro(v): return v


# ───────────────── #3a: status=delivering ДО send_message ─────────────────


@pytest.mark.asyncio
async def test_deliver_pins_sets_delivering_before_send_message(
    db_factory, monkeypatch
):
    """ДО вызова send_message статус заказа должен быть `delivering`.

    Это гарантирует, что если процесс упадёт между success send_message
    и commit'ом delivered — reconciler увидит delivering и НЕ повторит
    отправку автоматически.
    """
    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)
    await _seed_pins_ready_order(db_factory)

    fp = FakeFunPay()
    fp._db_factory = db_factory
    tg = FakeTelegram()

    async with db_factory() as s:
        existing = await find_order_by_funpay_id(s, "fp-twophase")
        assert existing is not None
        result = await proc._deliver_pins(
            _event(), existing, ["KEY-AAA"], fp, tg, proc.logger,
            ns_custom_id="ns-1", ns_price_usd=1.5,
        )

    assert result["status"] == "delivered"
    assert fp.status_during_send == ["delivering"], (
        f"в момент send_message статус должен был быть 'delivering', "
        f"а был {fp.status_during_send}"
    )

    async with db_factory() as s:
        o = await find_order_by_funpay_id(s, "fp-twophase")
        assert o is not None
        assert o.status == "delivered"


# ───────────────── #3b: откат на pins_ready при exception в send ─────────


@pytest.mark.asyncio
async def test_deliver_pins_rolls_back_to_pins_ready_on_send_failure(
    db_factory, monkeypatch
):
    """Если send_message бросил exception — сообщение НЕ дошло, статус
    должен откатиться на pins_ready (а не остаться delivering).

    Это позволяет reconciler / Retry безопасно повторить отправку.
    """
    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)
    await _seed_pins_ready_order(db_factory)

    fp = FakeFunPay(send_raises=RuntimeError("network down"))
    tg = FakeTelegram()

    async with db_factory() as s:
        existing = await find_order_by_funpay_id(s, "fp-twophase")
        assert existing is not None
        result = await proc._deliver_pins(
            _event(), existing, ["KEY-AAA"], fp, tg, proc.logger,
            ns_custom_id="ns-1", ns_price_usd=1.5,
        )

    assert result["status"] == "pins_ready"
    async with db_factory() as s:
        o = await find_order_by_funpay_id(s, "fp-twophase")
        assert o is not None
        assert o.status == "pins_ready", (
            "после fail send_message статус должен откатиться на pins_ready, "
            f"но остался {o.status}"
        )


# ───────────────── #3c: reconciler не повторяет delivering ─────────


@pytest.mark.asyncio
async def test_reconciler_does_not_retry_delivering_holds_manual_instead(
    db_factory, monkeypatch
):
    """Заказ в `delivering` означает «send_message успел уйти, но commit
    delivered не дошёл». Повторно отправлять НЕЛЬЗЯ (риск дубля).

    Reconciler должен перевести такой заказ в manual_hold + alert
    «проверьте чат вручную».
    """
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-stuck-delivering",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id="ns-1",
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="delivering",
                pins_json=json.dumps(["KEY-X"]),
                updated_at=datetime.utcnow() - timedelta(minutes=10),
            )
        )
        await s.commit()

    called: list[str] = []

    async def fake_process(event, **_kw):
        called.append(event.funpay_order_id)
        return {"status": "delivered"}

    monkeypatch.setattr(recon, "process_funpay_order", fake_process)

    holds: list[dict] = []

    async def fake_trigger_manual_hold(**kwargs):
        holds.append(kwargs)
        return {"status": "manual_hold"}

    monkeypatch.setattr(recon, "_trigger_manual_hold", fake_trigger_manual_hold)

    settings = type("Settings", (), {
        "order_reconcile_enabled": True,
        "order_reconcile_stale_after_seconds": 60,
        "order_reconcile_max_per_run": 10,
        "order_delivery_hard_timeout_seconds": 0,
    })()

    result = await recon.reconcile_orders_once(settings=settings)

    assert called == [], (
        "reconciler НЕ должен запускать process_funpay_order для delivering, "
        f"но запустил для: {called}"
    )
    assert len(holds) == 1
    assert holds[0]["funpay_order_id"] == "fp-stuck-delivering"
    assert holds[0]["stage"] == "reconciler_delivering_unclear"
    assert result["checked"] == 1
    assert result["skipped"] == 1


# ───────────────── #3d: ACTIVE_ORDER_STATUSES включает delivering ─────────


@pytest.mark.asyncio
async def test_active_order_statuses_includes_delivering(db_factory):
    """`delivering`-заказ должен считаться активным для help/hold/reserved."""
    from src.db.repo import ACTIVE_ORDER_STATUSES
    assert "delivering" in ACTIVE_ORDER_STATUSES, (
        "delivering должен быть в ACTIVE_ORDER_STATUSES, иначе:\n"
        " - help-cooldown пропустит этот заказ из active set\n"
        " - reserved_quantities_by_service не зарезервирует слот\n"
        " - list_active_orders_for_chat не покажет его hold'у"
    )
