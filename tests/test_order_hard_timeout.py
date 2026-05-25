"""
Тесты P2: hard-timeout, manual_hold вместо failed, race-guard,
description-persistence, reconciler с description.

Все интеграционные сценарии используют ту же in-memory SQLite, что и
test_order_processor.py — фикстуры тут продублированы намеренно, чтобы
этот файл можно было гонять независимо.

Сетевую часть не трогаем: NS/FunPay/Telegram заменены простыми
in-process фейками с тем же интерфейсом, что в test_order_processor.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base, Order
from src.db.repo import find_order_by_funpay_id, upsert_mapping
from src.ns.exceptions import NSOrderTimeoutError
from src.ns.models import CreateOrderResponse, OrderInfo, OrderStatus, PayOrderResponse
from src.orders import processor as proc
from src.orders import reconciler as recon
from src.orders.processor import FunPayOrderEvent, process_funpay_order


# ───────────────── фикстуры ─────────────────


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.orders.processor.session_factory", lambda: factory)
    monkeypatch.setattr("src.orders.reconciler.session_factory", lambda: factory)
    proc._order_locks.clear()
    yield factory
    await engine.dispose()


def _settings(**overrides) -> Settings:
    base: dict = dict(
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        enable_real_actions=True,
        # Дефолт для тестов: маленький timeout, чтобы легко его пробить
        # подменой Order.created_at в прошлое.
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


# ───────────────── фейки ─────────────────


class FakeNS:
    def __init__(
        self, *,
        custom_id: str = "ns-1",
        total: str = "1.5",
        pay_status: str = "completed",
        pay_pins: list[str] | None = None,
        wait_pins: list[str] | None = None,
        wait_raises_timeout: bool = False,
    ) -> None:
        self.created_calls = 0
        self.paid_calls = 0
        self.waited_calls = 0
        self.last_wait_timeout: float | None = None
        self._custom_id = custom_id
        self._total = total
        self._pay_status = pay_status
        self._pay_pins = pay_pins
        self._wait_pins = wait_pins
        self._wait_raises_timeout = wait_raises_timeout

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None

    async def create_order(self, *, service_id: int, fields, custom_id: str | None = None):
        self.created_calls += 1
        return CreateOrderResponse(
            custom_id=custom_id or self._custom_id, total_to_pay=self._total
        )

    async def pay_order(self, custom_id: str):
        self.paid_calls += 1
        return PayOrderResponse(
            custom_id=custom_id, status=self._pay_status, pins=self._pay_pins,
        )

    async def order_info(self, custom_id: str):
        from src.ns.exceptions import NSNotFoundError
        raise NSNotFoundError(404, "not found", path=f"/order_info/{custom_id}")

    async def wait_order_completion(
        self, custom_id: str, *, timeout_seconds: float | None = None
    ):
        self.waited_calls += 1
        self.last_wait_timeout = timeout_seconds
        if self._wait_raises_timeout:
            raise NSOrderTimeoutError(f"тест-таймаут для {custom_id}")
        return OrderInfo(
            custom_id=custom_id, status=OrderStatus.COMPLETED.value,
            status_message="ok", pins=self._wait_pins,
        )


class FakeFunPay:
    def __init__(self) -> None:
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

    @staticmethod
    async def _to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)


class FakeTelegram:
    """Фейк, поддерживающий и старый, и новый интерфейс TelegramNotifier."""

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

    async def manual_hold_required(self, **kw):
        self.manual_holds.append(kw)


# ───────────────── helpers ─────────────────


def _event(order_id: str = "fp-100", lot_id: int = 1001) -> FunPayOrderEvent:
    return FunPayOrderEvent(
        funpay_order_id=order_id,
        funpay_lot_id=lot_id,
        buyer_username="alice",
        buyer_user_id=42,
        chat_id=555,
        quantity=1,
        funpay_price_rub=200.0,
        description="Apple Gift Card 2 USD",
    )


async def _make_mapping(factory, *, lot_id: int = 1001) -> None:
    async with factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=lot_id, ns_service_id=20,
            markup_percent=5.0, stock_cap=10,
            ns_fields_template='{"quantity":"@QUANTITY"}',
            enabled=True, label="Apple USA 2",
        )
        await s.commit()


async def _force_order_age(factory, funpay_order_id: str, age_seconds: int) -> None:
    """Подменить created_at заказа на (now - age_seconds), чтобы пробить timeout."""
    async with factory() as s:
        order = await find_order_by_funpay_id(s, funpay_order_id)
        assert order is not None
        order.created_at = datetime.utcnow() - timedelta(seconds=age_seconds)
        await s.commit()


# ───────────────── description персистится ─────────────────


@pytest.mark.asyncio
async def test_order_description_persisted_to_db(db_factory, settings, monkeypatch):
    """Базовая проверка P2: description из event сохраняется в БД."""
    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)
    ns = FakeNS(pay_pins=["X1"])
    await process_funpay_order(
        _event(), settings=settings,
        ns_client=ns, funpay_client=FakeFunPay(), telegram=FakeTelegram(),
    )
    order = await _get_order(db_factory, "fp-100")
    assert order is not None
    assert order.description == "Apple Gift Card 2 USD"


# ───────────────── hard-timeout: 4 точки pipeline ─────────────────


@pytest.mark.asyncio
async def test_hard_timeout_before_ns_purchase(db_factory, settings):
    """Если заказу больше hard_timeout — НЕ покупаем в NS, ставим manual_hold."""
    await _make_mapping(db_factory)
    ns = FakeNS(pay_pins=["X1"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    # Создаём order вручную в "received" с очень старым created_at.
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-old",
                funpay_lot_id=1001,
                ns_service_id=20,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="received",
                created_at=datetime.utcnow() - timedelta(seconds=700),
            )
        )
        await s.commit()

    result = await process_funpay_order(
        _event(order_id="fp-old"), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "manual_hold"
    assert result.get("stage") == "before_ns_purchase"
    assert ns.created_calls == 0  # деньги НЕ списали
    assert ns.paid_calls == 0
    assert tg.manual_holds, "должен быть отправлен manual_hold_required alert"
    assert tg.manual_holds[0]["funpay_order_id"] == "fp-old"
    assert tg.manual_holds[0]["has_pins"] is False
    assert 1001 in fp.disabled_lots, "лот должен быть аварийно выключен"


@pytest.mark.asyncio
async def test_hard_timeout_before_wait_completion_skips_ns_wait(db_factory, settings):
    """Если до wait_completion осталось <=10s — сразу manual_hold, ns.wait не дёргается."""
    await _make_mapping(db_factory)
    # NS вернёт ns_paid, но без pins — нормально упадём в шаг wait_completion.
    ns = FakeNS(pay_status="in_progress", pay_pins=None, wait_pins=["LATE"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    # Первый прогон поставит заказ в ns_paid, оставив pins=[].
    # Но мы перехватываем сразу после create_order: подменим created_at
    # после первого вызова, чтобы при шаге 8 hard-timeout сработал.

    # Простой путь: настройки с очень коротким hard_timeout (= NS poll loop),
    # тогда при дёрге wait_completion remaining<=10.
    s_tight = _settings(order_delivery_hard_timeout_seconds=300)

    # Создаём заказ в ns_paid вручную, очень старый.
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-wait",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id="ns-1",
                ns_price_usd=1.5,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="ns_paid",
                created_at=datetime.utcnow() - timedelta(seconds=295),
            )
        )
        await s.commit()

    # Но process_funpay_order с existing ns_paid пойдёт через шаг 8
    # (без шага 4-7), если у нас НЕ pins_ready.
    # Проверяем: NS.wait не дёргается, статус manual_hold.
    result = await process_funpay_order(
        _event(order_id="fp-wait"),
        settings=s_tight, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "manual_hold"
    assert result.get("stage") == "ns_wait_completion"
    assert ns.waited_calls == 0, "wait_completion не должен был дёрнуться"
    assert tg.manual_holds


@pytest.mark.asyncio
async def test_ns_api_error_during_wait_becomes_manual_hold_with_alert(
    db_factory, settings
):
    """Аудит #6: NS 429 / NSAPIError из wait_completion ПОСЛЕ pay.

    Деньги уже списаны, pins не пришли, NS возвращает ошибку (например 429
    rate limit). До фикса: NSAPIError вылетал из processor, статус
    оставался ns_paid, Telegram-алерт НЕ уходил — оператор слепой.
    Теперь должен быть manual_hold + manual_hold_required alert.
    """
    from src.ns.exceptions import NSAPIError
    await _make_mapping(db_factory)

    class NSWith429(FakeNS):
        async def wait_order_completion(self, custom_id, *, timeout_seconds=None):
            raise NSAPIError(429, "rate limit", path=f"/order_info/{custom_id}")

    ns = NSWith429(pay_status="in_progress", pay_pins=None)
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-429"),
        settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "manual_hold", (
        f"NSAPIError ПОСЛЕ pay должен переводить в manual_hold, получено: {result}"
    )
    assert result.get("stage") == "ns_wait_completion"
    assert tg.manual_holds, (
        "После NSError ПОСЛЕ pay должен прилететь manual_hold_required alert"
    )


@pytest.mark.asyncio
async def test_ns_timeout_during_wait_becomes_manual_hold_not_failed(
    db_factory, settings
):
    """NSOrderTimeoutError из wait_completion должен переводить в manual_hold."""
    await _make_mapping(db_factory)
    ns = FakeNS(pay_status="in_progress", wait_raises_timeout=True)
    fp = FakeFunPay()
    tg = FakeTelegram()

    result = await process_funpay_order(
        _event(order_id="fp-nstimeout"),
        settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "manual_hold"
    assert result.get("stage") == "ns_wait_completion"
    assert ns.waited_calls == 1
    assert tg.manual_holds, "после NS-timeout должен прилететь manual_hold alert"

    order = await _get_order(db_factory, "fp-nstimeout")
    assert order is not None
    assert order.status == "manual_hold"
    # Аудит #1: ns_custom_id теперь deterministic = f"fp-{funpay_order_id}".
    assert order.ns_custom_id == "fp-fp-nstimeout"


@pytest.mark.asyncio
async def test_wait_completion_timeout_truncated_by_hard_timeout(db_factory, settings):
    """Если до hard-timeout осталось мало — передаём в wait_completion усечённый timeout."""
    await _make_mapping(db_factory)
    ns = FakeNS(pay_status="in_progress", wait_pins=["LATE-1"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    # ns_paid order с возрастом 100s, hard_timeout 600 → remaining 500.
    # ns_order_timeout_seconds по дефолту 600. Ожидаем min(600, 500) = 500.
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-tight",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id="ns-1",
                ns_price_usd=1.5,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="ns_paid",
                created_at=datetime.utcnow() - timedelta(seconds=100),
            )
        )
        await s.commit()

    await process_funpay_order(
        _event(order_id="fp-tight"),
        settings=settings, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert ns.waited_calls == 1
    assert ns.last_wait_timeout is not None
    # remaining = 600 - age (~100, чуть больше из-за выполнения теста) → ≈500
    assert 490 < ns.last_wait_timeout <= 600


@pytest.mark.asyncio
async def test_hard_timeout_post_pins_keeps_pins_and_holds(db_factory, monkeypatch):
    """Hard-timeout сработал ПОСЛЕ получения pins → pins сохранены, status=manual_hold."""
    await _make_mapping(db_factory)
    # Маленький hard_timeout, но не настолько, чтобы шаг 8 моментально дал manual_hold:
    # NS возвращает pins сразу через pay (без wait), значит шаг 8 не запускается.
    # Зато проверка post_pins_pre_delivery сработает, если age >= hard_timeout.
    s_tight = _settings(order_delivery_hard_timeout_seconds=300)
    ns = FakeNS(pay_pins=["KEEP-ME-1", "KEEP-ME-2"])
    fp = FakeFunPay()
    tg = FakeTelegram()

    # Заранее создаём очень старый received-заказ. before_ns_purchase
    # сработает РАНЬШЕ post_pins (мы достигнем его сразу), поэтому
    # для этого теста надо специально:
    #   - заказ возрастом 100s (меньше 300 hard timeout — пускаем покупку)
    #   - получаем pins
    #   - подменяем created_at на старый между шагами => симулируем
    # Проще: использовать monkeypatch на _is_hard_timeout, чтобы он
    # вернул False до получения pins и True после.
    calls = {"count": 0}
    real_is_timeout = proc._is_hard_timeout

    def fake_is_timeout(order, settings_arg, now=None):
        calls["count"] += 1
        # Первая проверка (before_ns_purchase) — False;
        # вторая (post_pins_pre_delivery) — True.
        return calls["count"] >= 2

    monkeypatch.setattr(proc, "_is_hard_timeout", fake_is_timeout)
    result = await process_funpay_order(
        _event(order_id="fp-post-pins"),
        settings=s_tight, ns_client=ns, funpay_client=fp, telegram=tg,
    )
    monkeypatch.setattr(proc, "_is_hard_timeout", real_is_timeout)

    assert result["status"] == "manual_hold"
    assert result.get("stage") == "post_pins_pre_delivery"
    assert ns.paid_calls == 1, "к этому моменту мы уже купили в NS"
    assert tg.manual_holds, "должен быть алерт"
    assert tg.manual_holds[0]["has_pins"] is True

    order = await _get_order(db_factory, "fp-post-pins")
    assert order is not None
    assert order.status == "manual_hold"
    assert order.pins_json is not None
    assert "KEEP-ME-1" in order.pins_json  # pins сохранены


# ───────────────── race-guard в _deliver_pins ─────────────────


@pytest.mark.asyncio
async def test_delivery_skipped_when_operator_marked_delivered(
    db_factory, settings, monkeypatch
):
    """
    Гонка: бот вошёл в _deliver_pins, в этот момент оператор в Telegram
    нажал «Выдано вручную» → status=delivered. Бот НЕ должен отправить дубль.
    """
    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)
    # Готовим заказ в pins_ready с сохранёнными pins.
    async with db_factory() as s:
        import json
        s.add(
            Order(
                funpay_order_id="fp-race",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id="ns-1",
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="delivered",  # уже delivered (оператор подтвердил)
                pins_json=json.dumps(["DUP-1"]),
                error="manual_delivered: оператор подтвердил",
            )
        )
        await s.commit()

    fp = FakeFunPay()
    tg = FakeTelegram()
    # process_funpay_order при status==delivered сразу выходит из шага 1.
    # Но мы хотим именно гонку в _deliver_pins — поэтому вызываем
    # _deliver_pins напрямую.
    async with db_factory() as s:
        existing = await find_order_by_funpay_id(s, "fp-race")
        assert existing is not None
        result = await proc._deliver_pins(
            _event(order_id="fp-race"),
            existing, ["DUP-1"], fp, tg, proc.logger,
            ns_custom_id="ns-1", ns_price_usd=1.5,
        )
    assert result["status"] == "delivered"
    assert result["skipped"] is True
    assert fp.sent == [], "send_message не должен был выполниться"


@pytest.mark.asyncio
async def test_force_delivery_does_not_override_operator_delivered(
    db_factory, settings, monkeypatch
):
    """force_delivery=True (Retry) НЕ переотправляет, если уже delivered."""
    monkeypatch.setattr(proc, "get_usd_rub_rate", lambda _s=None: _coro(100.0))
    await _make_mapping(db_factory)
    async with db_factory() as s:
        import json
        s.add(
            Order(
                funpay_order_id="fp-force-race",
                funpay_lot_id=1001,
                ns_service_id=20,
                ns_custom_id="ns-1",
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="delivered",
                pins_json=json.dumps(["X1"]),
            )
        )
        await s.commit()

    fp = FakeFunPay()
    tg = FakeTelegram()
    async with db_factory() as s:
        existing = await find_order_by_funpay_id(s, "fp-force-race")
        assert existing is not None
        result = await proc._deliver_pins(
            _event(order_id="fp-force-race"),
            existing, ["X1"], fp, tg, proc.logger,
            ns_custom_id="ns-1", ns_price_usd=1.5,
            force_delivery=True,
        )
    assert result["status"] == "delivered"
    assert result["skipped"] is True
    assert fp.sent == []


# ───────────────── manual_hold обработка существующего заказа ─────────────────


@pytest.mark.asyncio
async def test_process_funpay_order_exits_immediately_on_manual_hold(
    db_factory, settings
):
    """Повторный event для manual_hold заказа не запускает pipeline."""
    await _make_mapping(db_factory)
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-hold",
                funpay_lot_id=1001,
                ns_service_id=20,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="manual_hold",
                error="manual_hold: operator review",
            )
        )
        await s.commit()

    ns = FakeNS()
    fp = FakeFunPay()
    tg = FakeTelegram()
    result = await process_funpay_order(
        _event(order_id="fp-hold"), settings=settings,
        ns_client=ns, funpay_client=fp, telegram=tg,
    )
    assert result["status"] == "manual_hold"
    assert result.get("skipped") is True
    assert ns.created_calls == 0
    assert ns.paid_calls == 0
    assert fp.sent == []
    assert not tg.manual_holds  # повторный alert не шлём


# ───────────────── reconciler: description + skip too-old ─────────────────


@pytest.mark.asyncio
async def test_reconciler_passes_description_from_db_to_event(
    db_factory, monkeypatch
):
    """Reconciler читает Order.description и кладёт его в event."""
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-recon",
                funpay_lot_id=0,  # legacy без lot_id, маппинг через description
                ns_service_id=20,
                status="ns_paid",
                description="Apple Gift Card 5 USD",
                updated_at=datetime.utcnow() - timedelta(seconds=120),
                created_at=datetime.utcnow() - timedelta(seconds=120),
            )
        )
        await s.commit()

    captured: list[FunPayOrderEvent] = []

    async def fake_process(event, **_kwargs):
        captured.append(event)
        return {"status": "delivered"}

    monkeypatch.setattr(recon, "process_funpay_order", fake_process)

    settings = _settings(order_delivery_hard_timeout_seconds=600)
    result = await recon.reconcile_orders_once(settings=settings)
    assert result["recovered"] == 1
    assert len(captured) == 1
    assert captured[0].description == "Apple Gift Card 5 USD"
    assert captured[0].funpay_lot_id == 0


@pytest.mark.asyncio
async def test_reconciler_skips_orders_older_than_hard_timeout(
    db_factory, monkeypatch
):
    """Старые заказы переводятся в manual_hold вместо повторного pipeline."""
    async with db_factory() as s:
        s.add(
            Order(
                funpay_order_id="fp-too-old",
                funpay_lot_id=1001,
                ns_service_id=20,
                buyer_username="alice",
                chat_id=555,
                quantity=1,
                status="ns_paid",
                ns_custom_id="ns-1",
                description="Apple",
                updated_at=datetime.utcnow() - timedelta(seconds=1000),
                created_at=datetime.utcnow() - timedelta(seconds=1000),
            )
        )
        await s.commit()

    seen: list[str] = []

    async def fake_process(event, **_kwargs):
        seen.append(event.funpay_order_id)
        return {"status": "delivered"}

    monkeypatch.setattr(recon, "process_funpay_order", fake_process)

    fp = FakeFunPay()
    tg = FakeTelegram()
    settings = _settings(order_delivery_hard_timeout_seconds=600)
    result = await recon.reconcile_orders_once(
        settings=settings, funpay_client=fp, telegram=tg,
    )
    assert result["skipped"] == 1
    assert seen == [], "старый заказ НЕ должен идти в process_funpay_order"
    assert tg.manual_holds, "оператор должен получить alert"
    order = await _get_order(db_factory, "fp-too-old")
    assert order is not None
    assert order.status == "manual_hold"


# ───────────────── вспомогательные ─────────────────


async def _get_order(factory, funpay_order_id: str) -> Order | None:
    async with factory() as s:
        return await find_order_by_funpay_id(s, funpay_order_id)


async def _coro(value):
    return value
