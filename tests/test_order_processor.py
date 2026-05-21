"""
Тесты pipeline-а заказов.

Используют in-memory SQLite (file::memory:?cache=shared) и моки NS/FunPay
клиентов. Цель — проверить:
- идемпотентность (повторный вход не пересоздаёт NS-заказ);
- разделение pins_ready / delivered (если send_message клиенту падает,
  состояние остаётся pins_ready и при следующем входе доставка повторится);
- сериализацию по funpay_order_id (две параллельные обработки не плодят
  два NS-заказа).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base, ChatState, KnownLot, Mapping, Order
from src.db.repo import find_order_by_funpay_id, upsert_mapping
from src.ns.models import (
    CreateOrderResponse,
    OrderInfo,
    OrderStatus,
    PayOrderResponse,
)
from src.orders import processor as proc
from src.orders.processor import (
    FunPayOrderEvent,
    process_funpay_order,
)


# ─────────────── изолированная БД и сессии ───────────────


@pytest.fixture()
async def db_session_factory(monkeypatch):
    """In-memory SQLite на каждый тест, изолированная база."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # processor.session_factory() возвращает фабрику; подменяем модульно
    monkeypatch.setattr("src.orders.processor.session_factory", lambda: factory)

    # Чистим in-memory locks между тестами
    proc._order_locks.clear()

    yield factory
    await engine.dispose()


@pytest.fixture()
def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        enable_real_actions=True,
    )


# ─────────────── фейковые клиенты ───────────────


class FakeNS:
    """Имитирует NSClient: запоминает, сколько раз дёргали create/pay."""

    def __init__(
        self, *,
        custom_id: str = "ns-custom-1",
        total: str = "1.93",
        pay_status: str = "completed",
        pay_pins: list[str] | None = None,
        wait_status: int = OrderStatus.COMPLETED.value,
        wait_pins: list[str] | None = None,
    ) -> None:
        self.created_calls = 0
        self.paid_calls = 0
        self.waited_calls = 0
        self._custom_id = custom_id
        self._total = total
        self._pay_status = pay_status
        self._pay_pins = pay_pins
        self._wait_status = wait_status
        self._wait_pins = wait_pins

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None

    async def create_order(self, *, service_id: int, fields: list[dict]):
        self.created_calls += 1
        return CreateOrderResponse(
            custom_id=self._custom_id, total_to_pay=self._total
        )

    async def pay_order(self, custom_id: str):
        self.paid_calls += 1
        return PayOrderResponse(
            custom_id=custom_id,
            status=self._pay_status,  # type: ignore[arg-type]
            pins=self._pay_pins,
        )

    async def wait_order_completion(self, custom_id: str):
        self.waited_calls += 1
        return OrderInfo(
            custom_id=custom_id, status=self._wait_status,
            status_message="ok", pins=self._wait_pins,
        )


class FakeFunPay:
    """Имитирует FunPayClient.send_message; можно сделать падающим."""

    def __init__(
        self, *, fail: bool = False, fail_times: int = 0,
        chat_by_name_id: int | None = None,
    ):
        self.sent: list[tuple[int, str]] = []
        self._fail = fail
        self._fail_times = fail_times
        self.account = self
        self._chat_by_name_id = chat_by_name_id
        self.saved_lots: list[Any] = []
        self.disabled_lots: list[int] = []

    async def send_message(self, chat_id: int, text: str):
        if self._fail or self._fail_times > 0:
            self._fail_times = max(0, self._fail_times - 1)
            raise RuntimeError("FunPay send_message моковый сбой")
        self.sent.append((chat_id, text))

    async def get_lot_fields(self, lot_id: int):
        class Lot:
            def __init__(self, lot_id: int):
                self.lot_id = lot_id
                self.active = True
                self.amount = 100
                self.price = 147
        return Lot(lot_id)

    async def save_lot(self, lot_fields):
        self.saved_lots.append(lot_fields)
        if getattr(lot_fields, "active", True) is False:
            self.disabled_lots.append(lot_fields.lot_id)
        return {"ok": True}

    @staticmethod
    async def _to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def get_chat_by_name(self, username: str, make_request: bool = False):
        if self._chat_by_name_id is None:
            return None
        class Chat:
            id = self._chat_by_name_id
        return Chat()


class FakeTelegram:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.successes: list[dict] = []
        self.failures: list[dict] = []

    async def error(self, text: str): self.errors.append(text)
    async def warning(self, text: str): self.warnings.append(text)
    async def order_success(self, **kw): self.successes.append(kw)
    async def order_failure(self, **kw): self.failures.append(kw)


# ─────────────── helpers ───────────────


def _event(order_id: str = "fp-100", lot_id: int = 69300023) -> FunPayOrderEvent:
    return FunPayOrderEvent(
        funpay_order_id=order_id,
        funpay_lot_id=lot_id,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=1,
        funpay_price_rub=200.0,
    )


async def _make_mapping(
    factory,
    *,
    lot_id: int = 69300023,
    svc_id: int = 20,
    label: str = "Test Apple USA 2 USD",
    ns_fields_template: str | None = '{"quantity":"@QUANTITY"}',
) -> None:
    async with factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=lot_id, ns_service_id=svc_id,
            markup_percent=6.0, stock_cap=10,
            ns_fields_template=ns_fields_template,
            enabled=True, label=label,
        )
        await s.commit()


async def _make_known_lot(factory, *, lot_id: int, title: str) -> None:
    async with factory() as s:
        s.add(KnownLot(funpay_lot_id=lot_id, title=title))
        await s.commit()


async def _order(factory, fp_order_id: str) -> Order | None:
    async with factory() as s:
        return await find_order_by_funpay_id(s, fp_order_id)


# ─────────────── собственно тесты ───────────────


@pytest.mark.asyncio
async def test_happy_path_pay_returns_pins_immediately(
    db_session_factory, settings, monkeypatch
):
    async def fake_rate(_settings=None):
        return 100.0

    monkeypatch.setattr(proc, "get_usd_rub_rate", fake_rate)
    await _make_mapping(db_session_factory)
    ns = FakeNS(pay_pins=["AAAA-AAAA"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(), settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "delivered"
    assert result["pins_count"] == 1
    assert ns.created_calls == 1
    assert ns.paid_calls == 1
    assert ns.waited_calls == 0  # pins пришли сразу, ждать не надо
    assert len(fp.sent) == 2  # приветствие + доставка
    assert tg.successes and not tg.failures

    db_order = await _order(db_session_factory, "fp-100")
    assert db_order is not None
    assert db_order.status == "delivered"
    assert db_order.ns_custom_id == "ns-custom-1"
    assert db_order.fx_rate_at_sale == 100.0
    assert db_order.profit_rub == pytest.approx(
        db_order.funpay_price_rub * 0.97 - db_order.ns_price_usd * 100.0
    )
    assert db_order.profit_margin_percent == pytest.approx(
        db_order.profit_rub / db_order.funpay_price_rub * 100.0
    )


@pytest.mark.asyncio
async def test_multi_quantity_propagates_to_ns_and_delivers_all_pins(
    db_session_factory, settings
):
    """
    Покупатель купил 2 единицы на FunPay → NS получает quantity=2 →
    возвращает 2 пина → клиент получает оба в одном сообщении.
    """
    await _make_mapping(db_session_factory)

    captured_fields: list[list[dict]] = []

    class FakeNSCapturing(FakeNS):
        async def create_order(self, *, service_id: int, fields):
            captured_fields.append(list(fields))
            return await super().create_order(service_id=service_id, fields=fields)

    ns = FakeNSCapturing(pay_pins=["AAAA-1111", "BBBB-2222"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    event = FunPayOrderEvent(
        funpay_order_id="fp-multi",
        funpay_lot_id=69300023,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=2,
        funpay_price_rub=371.82,
    )

    result = await process_funpay_order(
        event, settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "delivered"
    assert result["pins_count"] == 2

    # NS получил quantity=2 в fields
    assert captured_fields, "create_order не был вызван"
    assert captured_fields[0] == [{"key": "quantity", "value": 2}], (
        f"Ожидал quantity=2, получил: {captured_fields[0]}"
    )

    # Оба пина в одном сообщении доставки
    delivery_msg = fp.sent[-1][1]
    assert "AAAA-1111" in delivery_msg
    assert "BBBB-2222" in delivery_msg


@pytest.mark.asyncio
async def test_order_without_lot_id_uses_single_enabled_mapping(
    db_session_factory, settings
):
    """
    FunPayAPI NewOrderEvent приходит как OrderShortcut без lot_id.
    Для тестового режима с одним активным маппингом processor должен
    сопоставить заказ и не пропустить покупку.
    """
    await _make_mapping(db_session_factory, lot_id=69300023)
    ns = FakeNS(pay_pins=["AAAA-1111"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    event = FunPayOrderEvent(
        funpay_order_id="fp-no-lot",
        funpay_lot_id=0,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=1,
        funpay_price_rub=200.0,
        description="Apple Gift Card | USA | 2 USD",
    )

    result = await process_funpay_order(
        event, settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    db_order = await _order(db_session_factory, "fp-no-lot")
    assert db_order is not None
    assert db_order.funpay_lot_id == 69300023


@pytest.mark.asyncio
async def test_order_without_lot_id_matches_russian_description_among_mappings(
    db_session_factory, settings
):
    """
    Реальный FunPay NewOrderEvent часто приходит без lot_id, но с русским
    описанием лота. Даже если label маппинга англоязычный, должны выбрать
    правильный Apple 2 USD, а не упасть с "нет маппинга для lot_id=0".
    """
    await _make_mapping(
        db_session_factory,
        lot_id=69300023,
        svc_id=20,
        label="Apple Gift Card | USA | 2 USD",
    )
    await _make_mapping(
        db_session_factory,
        lot_id=69300024,
        svc_id=21,
        label="Apple Gift Card | USA | 5 USD",
    )
    ns = FakeNS(pay_pins=["AAAA-1111"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    event = FunPayOrderEvent(
        funpay_order_id="fp-russian-desc",
        funpay_lot_id=0,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=1,
        funpay_price_rub=147.0,
        description=(
            "✈️АВТОВЫДАЧА 🔑 Подарочная карта Apple 🔵 2 USD "
            "(США) 🔵, USD, 2 USD"
        ),
    )

    result = await process_funpay_order(
        event, settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    db_order = await _order(db_session_factory, "fp-russian-desc")
    assert db_order is not None
    assert db_order.funpay_lot_id == 69300023
    assert db_order.ns_service_id == 20


@pytest.mark.asyncio
async def test_order_without_lot_id_can_match_known_lot_title(
    db_session_factory, settings
):
    await _make_mapping(
        db_session_factory,
        lot_id=69300023,
        svc_id=20,
        label="Service #20",
    )
    await _make_mapping(
        db_session_factory,
        lot_id=69300024,
        svc_id=21,
        label="Service #21",
    )
    await _make_known_lot(
        db_session_factory,
        lot_id=69300023,
        title="✈️АВТОВЫДАЧА 🔑 Подарочная карта Apple 🔵 2 USD (США)",
    )
    await _make_known_lot(
        db_session_factory,
        lot_id=69300024,
        title="✈️АВТОВЫДАЧА 🔑 Подарочная карта Apple 🔵 5 USD (США)",
    )
    ns = FakeNS(pay_pins=["AAAA-1111"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    event = FunPayOrderEvent(
        funpay_order_id="fp-known-title",
        funpay_lot_id=0,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=1,
        funpay_price_rub=147.0,
        description="Подарочная карта Apple 2 USD США, USD, 2 USD",
    )

    result = await process_funpay_order(
        event, settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    db_order = await _order(db_session_factory, "fp-known-title")
    assert db_order is not None
    assert db_order.funpay_lot_id == 69300023


@pytest.mark.asyncio
async def test_order_without_lot_id_distinguishes_same_amount_by_currency(
    db_session_factory, settings
):
    """
    При выкладке разных валют одинаковый номинал не должен смешиваться:
    Battle.net 5 USD и 5 EUR похожи почти полностью, поэтому токен валюты
    обязан участвовать в fallback-сопоставлении order без lot_id.
    """
    await _make_mapping(
        db_session_factory,
        lot_id=69406129,
        svc_id=196,
        label="Blizzard Gift Card | US | 5 USD",
    )
    await _make_mapping(
        db_session_factory,
        lot_id=69406130,
        svc_id=197,
        label="Blizzard Gift Card | EU | 5 EUR",
    )
    ns = FakeNS(pay_pins=["BATTLE-USD-5"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    event = FunPayOrderEvent(
        funpay_order_id="fp-bnet-usd",
        funpay_lot_id=0,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=1,
        funpay_price_rub=383.0,
        description="Подарочная карта Battle.net 5 USD (США), USD, 5 USD",
    )

    result = await process_funpay_order(
        event, settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    db_order = await _order(db_session_factory, "fp-bnet-usd")
    assert db_order is not None
    assert db_order.funpay_lot_id == 69406129
    assert db_order.ns_service_id == 196


@pytest.mark.asyncio
async def test_order_without_chat_id_resolves_chat_by_buyer_name(
    db_session_factory, settings
):
    await _make_mapping(db_session_factory, lot_id=69300023)
    ns = FakeNS(pay_pins=["AAAA-1111"])
    fp = FakeFunPay(chat_by_name_id=777)
    tg = FakeTelegram()

    event = FunPayOrderEvent(
        funpay_order_id="fp-no-chat",
        funpay_lot_id=69300023,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=None,
        quantity=1,
        funpay_price_rub=200.0,
        description="Apple Gift Card | USA | 2 USD",
    )

    result = await process_funpay_order(
        event, settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "delivered"
    assert fp.sent and all(chat_id == 777 for chat_id, _ in fp.sent)


@pytest.mark.asyncio
async def test_pay_returns_in_progress_then_wait(db_session_factory, settings):
    """Если pay_order вернул pending — должен дёрнуться wait_order_completion."""
    await _make_mapping(db_session_factory)
    ns = FakeNS(
        pay_status="in_progress", pay_pins=None,
        wait_pins=["BBBB-BBBB", "CCCC-CCCC"],
    )
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(), settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "delivered"
    assert result["pins_count"] == 2
    assert ns.waited_calls == 1


@pytest.mark.asyncio
async def test_delivery_failure_keeps_pins_ready_for_retry(
    db_session_factory, settings,
):
    """
    Если FunPay-чат недоступен при доставке, статус остаётся pins_ready.
    Повторный вход не дёргает NS заново, а только пытается доставить.
    """
    await _make_mapping(db_session_factory)
    ns = FakeNS(pay_pins=["XXXX-YYYY"])
    fp_fail = FakeFunPay(fail=True)
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(), settings=settings, ns_client=ns,
        funpay_client=fp_fail, telegram=tg,
    )
    assert result["status"] == "pins_ready"
    assert ns.paid_calls == 1
    assert fp_fail.disabled_lots == [69300023]
    assert tg.warnings
    async with db_session_factory() as s:
        mapping = (
            await s.execute(
                proc.select(Mapping).where(Mapping.funpay_lot_id == 69300023)
            )
        ).scalar_one()
        assert mapping.enabled is False
    db_order = await _order(db_session_factory, "fp-100")
    assert db_order is not None
    assert db_order.status == "pins_ready"
    assert db_order.pins_json is not None
    assert "XXXX-YYYY" in db_order.pins_json

    # Re-entry с уже рабочим FunPay
    fp_ok = FakeFunPay()
    result2 = await process_funpay_order(
        _event(), settings=settings, ns_client=ns,
        funpay_client=fp_ok, telegram=tg,
    )
    assert result2["status"] == "delivered"
    # NS не должен быть вызван повторно
    assert ns.created_calls == 1
    assert ns.paid_calls == 1
    # Клиенту в этот раз отправлено сообщение с кодом
    assert any("XXXX-YYYY" in body for _, body in fp_ok.sent)


@pytest.mark.asyncio
async def test_help_request_holds_order_after_grace_before_auto_delivery(
    db_session_factory, settings,
):
    await _make_mapping(db_session_factory)
    async with db_session_factory() as session:
        state = ChatState(chat_id=555, buyer_username="alice")
        session.add(state)
        order = Order(
            funpay_order_id="fp-help-hold",
            funpay_lot_id=69300023,
            ns_service_id=20,
            ns_custom_id="ns-help-hold",
            buyer_username="alice",
            buyer_user_id=42,
            chat_id=555,
            quantity=1,
            funpay_price_rub=200.0,
            ns_price_usd=1.93,
            status="ns_paid",
        )
        session.add(order)
        await session.flush()
        order.created_at = datetime.utcnow() - timedelta(minutes=8)
        state.last_help_request_at = order.created_at + timedelta(minutes=1)
        await session.commit()

    ns = FakeNS(wait_pins=["HOLD-PIN"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-help-hold"),
        settings=settings,
        ns_client=ns,
        funpay_client=fp,
        telegram=tg,
    )

    assert result["status"] == "manual_hold"
    assert fp.sent == []
    assert tg.warnings
    db_order = await _order(db_session_factory, "fp-help-hold")
    assert db_order is not None
    assert db_order.status == "manual_hold"
    assert db_order.pins_json is not None
    assert "HOLD-PIN" in db_order.pins_json


@pytest.mark.asyncio
async def test_help_request_allows_auto_delivery_during_grace(
    db_session_factory, settings,
):
    await _make_mapping(db_session_factory)
    async with db_session_factory() as session:
        state = ChatState(chat_id=555, buyer_username="alice")
        session.add(state)
        order = Order(
            funpay_order_id="fp-help-grace",
            funpay_lot_id=69300023,
            ns_service_id=20,
            ns_custom_id="ns-help-grace",
            buyer_username="alice",
            buyer_user_id=42,
            chat_id=555,
            quantity=1,
            funpay_price_rub=200.0,
            ns_price_usd=1.93,
            status="ns_paid",
        )
        session.add(order)
        await session.flush()
        state.last_help_request_at = order.created_at
        await session.commit()

    ns = FakeNS(wait_pins=["GRACE-PIN"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-help-grace"),
        settings=settings,
        ns_client=ns,
        funpay_client=fp,
        telegram=tg,
    )

    assert result["status"] == "delivered"
    assert any("GRACE-PIN" in body for _, body in fp.sent)
    assert tg.warnings == []
    db_order = await _order(db_session_factory, "fp-help-grace")
    assert db_order is not None
    assert db_order.status == "delivered"


@pytest.mark.asyncio
async def test_order_failure_disables_lot_before_more_buyers(
    db_session_factory, settings,
):
    """
    Если заказ не может быть продолжен до выдачи, лот надо аварийно выключить,
    чтобы другие покупатели не купили тот же проблемный товар.
    """
    await _make_mapping(
        db_session_factory,
        ns_fields_template="{not json",
    )
    ns = FakeNS()
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(), settings=settings, ns_client=ns,
        funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "failed"
    assert fp.disabled_lots == [69300023]
    assert tg.failures
    assert tg.warnings
    async with db_session_factory() as s:
        mapping = (
            await s.execute(
                proc.select(Mapping).where(Mapping.funpay_lot_id == 69300023)
            )
        ).scalar_one()
        assert mapping.enabled is False


@pytest.mark.asyncio
async def test_failure_with_unknown_lot_id_does_not_disable_random_lot(
    db_session_factory, settings,
):
    ns = FakeNS()
    fp = FakeFunPay()
    tg = FakeTelegram()

    event = FunPayOrderEvent(
        funpay_order_id="fp-no-map",
        funpay_lot_id=0,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=1,
        funpay_price_rub=200.0,
        description="unknown product",
    )

    result = await process_funpay_order(
        event, settings=settings, ns_client=ns,
        funpay_client=fp, telegram=tg,
    )

    assert result["status"] == "failed"
    assert fp.disabled_lots == []


@pytest.mark.asyncio
async def test_reentry_on_delivered_is_noop(db_session_factory, settings):
    await _make_mapping(db_session_factory)
    ns = FakeNS(pay_pins=["ZZZZ"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    await process_funpay_order(
        _event(), settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    # Второй заход
    ns.created_calls = 0
    ns.paid_calls = 0
    fp.sent.clear()
    result = await process_funpay_order(
        _event(), settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "delivered"
    assert result["skipped"] is True
    assert ns.created_calls == 0
    assert ns.paid_calls == 0
    assert fp.sent == []


@pytest.mark.asyncio
async def test_no_mapping_marks_failed(db_session_factory, settings):
    ns = FakeNS()
    fp = FakeFunPay()
    tg = FakeTelegram()
    result = await process_funpay_order(
        _event(lot_id=999999), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "failed"
    assert ns.created_calls == 0
    assert len(tg.failures) == 1


@pytest.mark.asyncio
async def test_parallel_calls_serialised_by_lock(db_session_factory, settings):
    """Два параллельных process_funpay_order для одного и того же id
    должны выполняться последовательно — NS-заказ создаётся ровно один раз."""
    await _make_mapping(db_session_factory)
    ns = FakeNS(pay_pins=["SAME-PIN"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    ev = _event()
    results = await asyncio.gather(
        process_funpay_order(
            ev, settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
        ),
        process_funpay_order(
            ev, settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
        ),
    )
    statuses = sorted(r["status"] for r in results)
    # Первый завершит «delivered», второй увидит «delivered» и пропустит
    assert statuses == ["delivered", "delivered"]
    # NS create_order ровно один раз
    assert ns.created_calls == 1
    assert ns.paid_calls == 1


@pytest.mark.asyncio
async def test_dry_run_creates_but_not_pays(db_session_factory, settings):
    await _make_mapping(db_session_factory)
    ns = FakeNS()
    fp = FakeFunPay()
    tg = FakeTelegram()
    result = await process_funpay_order(
        _event(), settings=settings, ns_client=ns, funpay_client=fp,
        telegram=tg, dry_run=True,
    )
    assert result["status"] == "ns_created"
    assert result["dry_run"] is True
    assert ns.created_calls == 1
    assert ns.paid_calls == 0
