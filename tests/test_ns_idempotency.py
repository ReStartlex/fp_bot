"""
Аудит #1: NS create_order/pay_order idempotency.

Сценарии до фикса:
  A) create_order успешен в NS (custom_id записан там), но crash до
     commit'a в БД → retry создаёт ВТОРОЙ заказ в NS с новым UUID.
     Старый висит в NS, новый создаётся. Потенциал двойной покупки.

  B) pay_order успешен в NS (деньги списаны), но crash до commit'a
     ns_paid → retry повторно вызывает pay_order. Если NS не идемпотентен,
     возможно ДВОЙНОЕ списание.

Фикс:
  1. NS требует UUID4 (с 2026-05-25, см. test_orders_uuid_custom_id.py).
     Раньше использовали deterministic f"fp-{funpay_order_id}"; теперь —
     uuid.uuid4(), который пишется в БД ДО вызова NS как intent marker.
     При retry (без commit'a status'a) мы видим тот же UUID в БД и
     обращаемся к ТОМУ ЖЕ NS-заказу через order_info.
  2. Перед create_order: order_info(custom_id). Если найден — пропустить
     create.
  3. Перед pay_order: order_info(custom_id). Если status != CREATED —
     пропустить pay (заказ уже в обработке).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base, Order
from src.db.repo import find_order_by_funpay_id, upsert_mapping
from src.ns.exceptions import NSNotFoundError
from src.ns.models import (
    CreateOrderResponse,
    OrderInfo,
    OrderStatus,
    PayOrderResponse,
)
from src.orders import processor as proc
from src.orders.processor import FunPayOrderEvent, process_funpay_order


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.orders.processor.session_factory", lambda: factory)
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


class FakeNSIdempotent:
    """NS-фейк с поведением, эмулирующим идемпотентные ответы."""

    def __init__(
        self,
        *,
        existing_orders: dict[str, OrderInfo] | None = None,
        pay_pins: list[str] | None = None,
    ) -> None:
        self.created_calls: list[tuple[int, str]] = []
        self.paid_calls: list[str] = []
        self.order_info_calls: list[str] = []
        self.existing: dict[str, OrderInfo] = existing_orders or {}
        self._pay_pins = pay_pins or ["PIN-1"]

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None

    async def create_order(self, *, service_id: int, fields, custom_id: str | None = None):
        assert custom_id is not None, (
            "Аудит #1: NSClient.create_order должен вызываться с явным "
            "custom_id (deterministic), иначе при retry будет создан "
            "новый заказ с новым UUID = дубль."
        )
        self.created_calls.append((service_id, custom_id))
        self.existing[custom_id] = OrderInfo(
            custom_id=custom_id, status=OrderStatus.CREATED.value,
            status_message="created", total_price=1.5,
        )
        return CreateOrderResponse(custom_id=custom_id, total_to_pay="1.5")

    async def pay_order(self, custom_id: str):
        self.paid_calls.append(custom_id)
        info = self.existing.get(custom_id)
        if info is not None:
            self.existing[custom_id] = OrderInfo(
                custom_id=custom_id, status=OrderStatus.COMPLETED.value,
                status_message="completed", total_price=info.total_price,
                pins=self._pay_pins,
            )
        return PayOrderResponse(custom_id=custom_id, status="completed", pins=self._pay_pins)

    async def order_info(self, custom_id: str):
        self.order_info_calls.append(custom_id)
        info = self.existing.get(custom_id)
        if info is None:
            raise NSNotFoundError(404, "not found", path=f"/order_info/{custom_id}")
        return info

    async def wait_order_completion(self, custom_id: str, *, timeout_seconds=None):
        info = self.existing.get(custom_id)
        if info is None:
            raise NSNotFoundError(404, "not found", path=f"/order_info/{custom_id}")
        return info


class FakeFunPay:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.account = self
        self.saved_lots: list[Any] = []
        self.disabled_lots: list[int] = []

    async def send_message(self, chat_id: int, text: str):
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

    async def warning(self, text): self.warnings.append(text)
    async def error(self, text): self.errors.append(text)
    async def order_success(self, **kw): self.successes.append(kw)
    async def order_failure(self, **kw): self.failures.append(kw)
    async def manual_hold_required(self, **kw): self.manual_holds.append(kw)


def _event(order_id: str = "fp-X1") -> FunPayOrderEvent:
    return FunPayOrderEvent(
        funpay_order_id=order_id,
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


async def _coro(v): return v


# ───────────────── #1a: deterministic custom_id ─────────────────


@pytest.mark.asyncio
async def test_create_order_uses_valid_uuid4(
    db_factory, settings, monkeypatch
):
    """custom_id должен быть валидным UUID4 (NS-требование от 2026-05-25).
    И сохранён в БД для idempotency при retry."""
    from src.orders.processor import _is_valid_uuid4

    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)

    ns = FakeNSIdempotent()
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-X1"), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    assert len(ns.created_calls) == 1
    _, used_custom_id = ns.created_calls[0]
    assert _is_valid_uuid4(used_custom_id), (
        f"NS отвергает не-UUID4: {used_custom_id!r}"
    )
    # И тот же UUID должен быть в БД (intent marker)
    async with db_factory() as s:
        order = await find_order_by_funpay_id(s, "fp-X1")
        assert order is not None
        assert order.ns_custom_id == used_custom_id


# ───────────────── #1b: retry create_order не создаёт дубль ─────────


@pytest.mark.asyncio
async def test_retry_does_not_recreate_existing_ns_order(
    db_factory, settings, monkeypatch
):
    """Сценарий A: predыдущая попытка create_order дошла до NS, но crash
    случился до commit'a ns_created в БД (статус остался received).
    Retry должен через order_info обнаружить заказ и НЕ создавать дубль.
    """
    import uuid

    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)

    # Intent marker UUID4 от прошлой попытки (NS-формат)
    saved_uuid = str(uuid.uuid4())

    # БД: статус received, ns_custom_id уже сохранён (intent marker от прошлой попытки).
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-X2",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id=saved_uuid,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="received",
            )
        )
        await s.commit()

    # NS: заказ уже существует с этим custom_id (предыдущий create успел).
    ns = FakeNSIdempotent(
        existing_orders={
            saved_uuid: OrderInfo(
                custom_id=saved_uuid, status=OrderStatus.CREATED.value,
                status_message="created", total_price=1.5,
            )
        }
    )
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-X2"), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    assert len(ns.created_calls) == 0, (
        f"create_order НЕ должен вызываться при существующем заказе в NS, "
        f"вызовы: {ns.created_calls}"
    )
    # order_info должен был вызваться хотя бы один раз для проверки.
    assert saved_uuid in ns.order_info_calls


# ───────────────── #1c: retry pay_order не оплачивает повторно ─────────


@pytest.mark.asyncio
async def test_retry_does_not_repay_when_ns_order_already_in_progress(
    db_factory, settings, monkeypatch
):
    """Сценарий B: pay_order дошёл до NS (статус IN_PROGRESS или COMPLETED),
    но crash до commit'a ns_paid в БД. Retry должен через order_info
    опознать что заказ уже оплачен и НЕ вызывать pay_order повторно."""
    import uuid
    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)

    saved_uuid = str(uuid.uuid4())

    # БД: статус ns_created (commit ns_paid не дошёл).
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-X3",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id=saved_uuid,
                ns_price_usd=1.5,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="ns_created",
            )
        )
        await s.commit()

    # NS: заказ уже COMPLETED с pins.
    ns = FakeNSIdempotent(
        existing_orders={
            saved_uuid: OrderInfo(
                custom_id=saved_uuid, status=OrderStatus.COMPLETED.value,
                status_message="completed", total_price=1.5,
                pins=["RECOVERED-PIN"],
            )
        }
    )
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-X3"), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    assert ns.paid_calls == [], (
        f"pay_order НЕ должен вызываться при уже завершённом заказе в NS, "
        f"вызовы: {ns.paid_calls}"
    )
    # Доставлены должны быть восстановленные pins из NS.
    assert any("RECOVERED-PIN" in text for _, text in fp.sent), fp.sent


# ───────────────── #1d: 404 от order_info → нормальный create ─────────


@pytest.mark.asyncio
async def test_create_order_proceeds_when_ns_returns_404(
    db_factory, settings, monkeypatch
):
    """Если ns_custom_id есть в БД (intent marker, UUID4), но NS возвращает
    404 (предыдущий create НЕ дошёл) — должны нормально вызвать create_order
    с тем же UUID."""
    import uuid
    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)

    saved_uuid = str(uuid.uuid4())

    # БД: ns_custom_id сохранён, но в NS заказа НЕТ.
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-X4",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id=saved_uuid,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="received",
            )
        )
        await s.commit()

    ns = FakeNSIdempotent()  # пусто
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-X4"), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    assert len(ns.created_calls) == 1
    # Тот же UUID4 из БД (не перегенерён, потому что он валидный)
    assert ns.created_calls[0][1] == saved_uuid


# ───────────────── #1e: intent marker сохранён ДО NS-вызова ─────────


@pytest.mark.asyncio
async def test_ns_custom_id_persisted_before_ns_call(
    db_factory, settings, monkeypatch
):
    """custom_id должен сохраняться в БД ДО вызова create_order. Это
    intent marker: если NS-вызов крашнется до ответа, retry увидит
    custom_id и сможет проверить order_info.

    Проверяем перехватом: подменяем create_order на функцию, которая ДО
    обращения к NS читает текущее состояние БД."""
    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)

    db_state_at_create: dict = {}

    class CapturingNS(FakeNSIdempotent):
        async def create_order(self_inner, *, service_id, fields, custom_id=None):
            # В этот момент проверим: в БД уже должен быть intent marker.
            async with db_factory() as s:
                o = await find_order_by_funpay_id(s, "fp-X5")
                db_state_at_create["ns_custom_id"] = o.ns_custom_id if o else None
            return await FakeNSIdempotent.create_order(
                self_inner, service_id=service_id, fields=fields, custom_id=custom_id
            )

    ns = CapturingNS()
    fp = FakeFunPay()
    tg = FakeTelegram()

    await process_funpay_order(
        _event(order_id="fp-X5"), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )

    from src.orders.processor import _is_valid_uuid4
    saved = db_state_at_create["ns_custom_id"]
    assert _is_valid_uuid4(saved), (
        "ns_custom_id должен быть сохранён в БД ДО вызова NS.create_order "
        f"и быть валидным UUID4 (NS-требование), но было: {saved!r}"
    )


# ───────────────── #1f: legacy "fp-..." перегенерируется в UUID4 ───────


@pytest.mark.asyncio
async def test_legacy_fp_id_in_db_is_regenerated_to_uuid4(
    db_factory, settings, monkeypatch
):
    """
    Регрессионный тест: до 2026-05-25 мы писали в БД custom_id вида
    `fp-WR3MVAKX`. После апгрейда NS отвергает такие id с 400. Код должен
    автоматически перегенерировать legacy-id в UUID4 при первой попытке
    обработки. Заказ при этом обрабатывается успешно.
    """
    from src.orders.processor import _is_valid_uuid4

    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)

    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-LEGACY",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id="fp-fp-LEGACY",  # legacy не-UUID4 формат
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="received",
            )
        )
        await s.commit()

    ns = FakeNSIdempotent()
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-LEGACY"), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    assert len(ns.created_calls) == 1
    used_id = ns.created_calls[0][1]
    assert _is_valid_uuid4(used_id), (
        f"Legacy 'fp-fp-LEGACY' должен был перегенериться в UUID4, "
        f"но в NS ушло: {used_id!r}"
    )
    assert used_id != "fp-fp-LEGACY"
    # И в БД теперь — новый UUID
    async with db_factory() as s:
        order = await find_order_by_funpay_id(s, "fp-LEGACY")
        assert order.ns_custom_id == used_id
