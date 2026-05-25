"""
Sprint 5 — тесты для src/shop/delivery.py

Подход: полностью мокаем NSClient (без httpx) и наблюдаем за переходами
ShopOrder через БД. notify_buyer / notify_owner — AsyncMock'и, проверяем
что бот написал что нужно.

Сценарии:
  * happy path: paid → delivering → delivered, pins сохранены, cashback
  * NS уже COMPLETED (idempotency на retry)
  * NS create_order упал → failed → refund
  * NS pay_order: insufficient funds → failed → refund
  * NS wait_completion timeout → остаётся pending (для retry)
  * NS возвращает REFUNDED → failed → refund
  * Заказ уже delivered — функция no-op
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db.models import Base, ShopBalanceLedger, ShopUser
from src.shop.checkout import attempt_checkout_via_balance
from src.shop.delivery import (
    DeliveryOutcome,
    _build_ns_fields,
    _format_pins_message,
    _is_valid_uuid4,
    deliver_shop_order_once,
    poll_shop_deliveries_once,
)
from src.shop.repo import (
    LEDGER_REASON_REFERRAL_CASHBACK,
    LEDGER_REASON_REFUND,
    SHOP_ORDER_STATUS_DELIVERED,
    SHOP_ORDER_STATUS_DELIVERING,
    SHOP_ORDER_STATUS_FAILED,
    SHOP_ORDER_STATUS_PAID,
    SHOP_ORDER_STATUS_REFUNDED,
    apply_balance_change,
    attach_referral,
    get_or_create_user,
    get_shop_order,
    list_orders_awaiting_delivery,
    upsert_catalog_service,
)
from src.shop.taxonomy import make_group_slug


# Импортируем модули, чтобы мочь подменить session_factory monkeypatch'ем.
import src.shop.delivery as delivery_mod
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture()
async def db_factory(monkeypatch):
    """
    SQLite in-memory + monkeypatch session_factory во ВСЕХ модулях
    которые её импортировали (delivery, repo, checkout).
    Это критично: модули хранят свою копию session_factory().
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # session_factory() возвращает sessionmaker. Сделаем wrapper.
    def fake_session_factory():
        return factory

    # Подменяем в delivery module
    monkeypatch.setattr(delivery_mod, "session_factory", fake_session_factory)
    yield factory
    await engine.dispose()


@pytest.fixture()
def settings():
    """Минимальные настройки для delivery."""
    return SimpleNamespace(
        ns_order_timeout_seconds=60,
        ns_order_poll_interval_seconds=1,
        shop_referral_percent=1.0,
    )


def make_ns_mock(
    *,
    order_info_response=None,
    order_info_raises=None,
    create_order_response=None,
    create_order_raises=None,
    pay_order_response=None,
    pay_order_raises=None,
    wait_completion_response=None,
    wait_completion_raises=None,
):
    """Конструирует фейковый NSClient с прописанными ответами/исключениями."""
    ns = SimpleNamespace()

    async def _order_info(custom_id):
        if order_info_raises:
            raise order_info_raises
        return order_info_response

    async def _create_order(service_id, fields, custom_id):
        if create_order_raises:
            raise create_order_raises
        return create_order_response

    async def _pay_order(custom_id):
        if pay_order_raises:
            raise pay_order_raises
        return pay_order_response

    async def _wait_completion(custom_id, *, timeout_seconds=None):
        if wait_completion_raises:
            raise wait_completion_raises
        return wait_completion_response

    ns.order_info = _order_info
    ns.create_order = _create_order
    ns.pay_order = _pay_order
    ns.wait_order_completion = _wait_completion
    return ns


def make_ns_info(status, pins=None, status_message=None, total_price=None):
    """Эмулирует OrderInfo с минимальным контрактом."""
    return SimpleNamespace(
        status_enum=status,
        pins=pins or [],
        status_message=status_message or "",
        total_price=total_price,
    )


def make_pay_resp(pins=None):
    return SimpleNamespace(status="paid", pins=pins or [])


# Простой substitute для OrderStatus enum — используем real одной import'ом
from src.ns.models import OrderStatus
from src.ns.exceptions import (
    NSAPIError,
    NSError,
    NSInsufficientFunds,
    NSNotFoundError,
    NSOrderTimeoutError,
)


def _not_found():
    """NSNotFoundError требует (status_code, message). Фабрика-хелпер."""
    return NSNotFoundError(404, "no order")


def _api_error(msg: str = "NS down"):
    return NSAPIError(500, msg)


def _timeout():
    """NSOrderTimeoutError — простое NSError без полей."""
    return NSOrderTimeoutError("wait timeout")


# ─── Setup helpers ────────────────────────────────────────────────


async def _setup_paid_order(
    factory, *, balance_kopecks=10000, price_kopecks=5000,
    with_inviter=False,
):
    """Создаёт user'а с балансом + paid-заказ через checkout."""
    async with factory() as s:
        buyer, _ = await get_or_create_user(s, telegram_user_id=200)
        if with_inviter:
            inviter, _ = await get_or_create_user(s, telegram_user_id=100)
            await attach_referral(
                s, referrer_user_id=inviter.id, referred_user_id=buyer.id,
            )
        await apply_balance_change(
            s, user_id=buyer.id, change_kopecks=balance_kopecks,
            reason="manual_topup",
        )
        apple_slug = make_group_slug("Apple Gift Card")
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US",
            service_name="Apple US $5",
            base_name="Apple Gift Card",
            group_slug=apple_slug,
            ns_price_usd=5.0,
            rub_price_kopecks=price_kopecks,
            in_stock=10, fields_json=None,
        )
        await s.commit()
    async with factory() as s:
        result = await attempt_checkout_via_balance(
            s, user_id=buyer.id, ns_service_id=1,
        )
        await s.commit()
    assert result.outcome.value == "ok"
    return buyer, result.order


# ─── Tests ────────────────────────────────────────────────────────


async def test_uuid_helpers():
    """_is_valid_uuid4 — sanity."""
    import uuid
    fresh = str(uuid.uuid4())
    assert _is_valid_uuid4(fresh)
    assert not _is_valid_uuid4("not-uuid")
    assert not _is_valid_uuid4(None)
    assert not _is_valid_uuid4("")


async def test_build_ns_fields():
    """_build_ns_fields парсит JSON, возвращает [] на мусор."""
    assert _build_ns_fields("[]") == []
    assert _build_ns_fields(None) == []
    assert _build_ns_fields("{garbage") == []
    assert _build_ns_fields('[{"name":"email"}]') == [{"name": "email"}]


async def test_format_pins_message_basic():
    order = SimpleNamespace(
        id=42, ns_service_name="Apple US $5",
    )
    pins = [{"pin": "ABC-123"}, {"pin": "XYZ-999", "serial": "S1"}]
    msg = _format_pins_message(order, pins)
    assert "#42" in msg
    assert "Apple US $5" in msg
    assert "ABC-123" in msg
    assert "XYZ-999" in msg
    assert "S1" in msg


# ─── Happy path ────────────────────────────────────────────────────


async def test_happy_path_delivers_and_credits_cashback(db_factory, settings):
    """paid → delivering → delivered + cashback инвайтеру."""
    buyer, order = await _setup_paid_order(
        db_factory, balance_kopecks=10000, price_kopecks=5000,
        with_inviter=True,
    )
    notify_buyer = AsyncMock()
    notify_owner = AsyncMock()

    pay_resp = make_pay_resp(pins=[{"pin": "MY-CODE-123"}])
    ns = make_ns_mock(
        order_info_raises=_not_found(),
        create_order_response=SimpleNamespace(total_to_pay=5.0),
        pay_order_response=pay_resp,
    )
    outcome = await deliver_shop_order_once(
        order.id, ns=ns, settings=settings,
        notify_buyer=notify_buyer, notify_owner=notify_owner,
    )
    assert outcome.delivered is True
    assert outcome.pins == [{"pin": "MY-CODE-123"}]
    assert outcome.cashback_credited_kopecks == 50  # 1% от 5000

    async with db_factory() as s:
        o = await get_shop_order(s, order.id)
    assert o.status == SHOP_ORDER_STATUS_DELIVERED
    assert "MY-CODE-123" in (o.pins_json or "")
    assert o.delivered_at is not None
    assert _is_valid_uuid4(o.ns_custom_id)

    # Buyer notification
    notify_buyer.assert_awaited_once()
    sent_user, sent_text = notify_buyer.await_args.args
    assert sent_user == buyer.id
    assert "MY-CODE-123" in sent_text


async def test_happy_path_no_inviter_no_cashback(db_factory, settings):
    """Покупатель без инвайтера — заказ доставлен, cashback = 0."""
    buyer, order = await _setup_paid_order(
        db_factory, balance_kopecks=10000, price_kopecks=5000,
        with_inviter=False,
    )

    pay_resp = make_pay_resp(pins=[{"pin": "XYZ-001"}])
    ns = make_ns_mock(
        order_info_raises=_not_found(),
        create_order_response=SimpleNamespace(total_to_pay=5.0),
        pay_order_response=pay_resp,
    )
    outcome = await deliver_shop_order_once(
        order.id, ns=ns, settings=settings,
    )
    assert outcome.delivered is True
    assert outcome.cashback_credited_kopecks == 0


# ─── Idempotency ──────────────────────────────────────────────────


async def test_idempotency_reuse_existing_ns_order(db_factory, settings):
    """
    NS уже знает наш заказ как COMPLETED (был retry после crash) —
    переиспользуем pins, без повторного create/pay.
    """
    buyer, order = await _setup_paid_order(
        db_factory, balance_kopecks=10000, price_kopecks=5000,
    )

    pre_info = make_ns_info(
        OrderStatus.COMPLETED, pins=[{"pin": "REUSED-123"}],
    )
    # Если create/pay вызвать — мы бы упали или зашунтировались.
    # Поставим Exception-raising create/pay чтобы проверить что они НЕ
    # вызывались.
    ns = make_ns_mock(
        order_info_response=pre_info,
        create_order_raises=AssertionError("create НЕ должен вызываться"),
        pay_order_raises=AssertionError("pay НЕ должен вызываться"),
    )

    outcome = await deliver_shop_order_once(
        order.id, ns=ns, settings=settings,
    )
    assert outcome.delivered is True
    assert outcome.pins == [{"pin": "REUSED-123"}]


async def test_already_delivered_is_noop(db_factory, settings):
    """Заказ уже delivered — функция не делает ничего."""
    buyer, order = await _setup_paid_order(
        db_factory, balance_kopecks=10000, price_kopecks=5000,
    )
    # Доставим один раз
    pay_resp = make_pay_resp(pins=[{"pin": "FIRST-1"}])
    ns = make_ns_mock(
        order_info_raises=_not_found(),
        create_order_response=SimpleNamespace(total_to_pay=5.0),
        pay_order_response=pay_resp,
    )
    await deliver_shop_order_once(order.id, ns=ns, settings=settings)

    # Теперь вторая попытка — NS вообще не должен вызываться
    ns2 = make_ns_mock(
        order_info_raises=AssertionError("второй вызов недопустим"),
        create_order_raises=AssertionError("create НЕ должен"),
        pay_order_raises=AssertionError("pay НЕ должен"),
    )
    outcome = await deliver_shop_order_once(
        order.id, ns=ns2, settings=settings,
    )
    assert outcome.delivered is False
    assert outcome.pending is False


# ─── Failure & refund ─────────────────────────────────────────────


async def test_create_order_failure_marks_failed_and_refunds(db_factory, settings):
    """create_order упал → failed → refund balance покупателю."""
    buyer, order = await _setup_paid_order(
        db_factory, balance_kopecks=10000, price_kopecks=5000,
    )
    # До delivery: balance = 5000 (10000 - 5000)
    async with db_factory() as s:
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == buyer.id)
        )).scalar_one()
    assert u.balance_kopecks == 5000

    notify_buyer = AsyncMock()
    notify_owner = AsyncMock()
    ns = make_ns_mock(
        order_info_raises=_not_found(),
        create_order_raises=_api_error("NS down"),
    )
    outcome = await deliver_shop_order_once(
        order.id, ns=ns, settings=settings,
        notify_buyer=notify_buyer, notify_owner=notify_owner,
    )
    assert outcome.failed is True
    assert "NS down" in (outcome.error or "")

    # Balance вернулся
    async with db_factory() as s:
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == buyer.id)
        )).scalar_one()
        o = await get_shop_order(s, order.id)
    assert u.balance_kopecks == 10000
    assert o.status == SHOP_ORDER_STATUS_REFUNDED

    # Buyer был уведомлён
    notify_buyer.assert_awaited()
    notify_owner.assert_awaited()


async def test_pay_order_insufficient_funds_refunds_buyer(db_factory, settings):
    """NS pay_order: insufficient — failed + refund."""
    buyer, order = await _setup_paid_order(
        db_factory, balance_kopecks=10000, price_kopecks=5000,
    )
    ns = make_ns_mock(
        order_info_raises=_not_found(),
        create_order_response=SimpleNamespace(total_to_pay=5.0),
        pay_order_raises=NSInsufficientFunds(custom_id="xxx", balance="1.0"),
    )
    outcome = await deliver_shop_order_once(
        order.id, ns=ns, settings=settings,
    )
    assert outcome.failed is True
    assert "недостаточно" in (outcome.error or "").lower()

    async with db_factory() as s:
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == buyer.id)
        )).scalar_one()
    assert u.balance_kopecks == 10000, "Refund вернул деньги"


async def test_wait_timeout_keeps_order_pending(db_factory, settings):
    """
    NS wait_order_completion timeout → НЕ failed, остаётся в delivering
    для retry на следующем тике. Деньги не возвращаются.
    """
    buyer, order = await _setup_paid_order(
        db_factory, balance_kopecks=10000, price_kopecks=5000,
    )
    ns = make_ns_mock(
        order_info_raises=_not_found(),
        create_order_response=SimpleNamespace(total_to_pay=5.0),
        pay_order_response=make_pay_resp(pins=[]),  # пинов нет сразу
        wait_completion_raises=_timeout(),
    )
    outcome = await deliver_shop_order_once(
        order.id, ns=ns, settings=settings,
    )
    assert outcome.delivered is False
    assert outcome.failed is False
    assert outcome.pending is True

    async with db_factory() as s:
        o = await get_shop_order(s, order.id)
        # Balance НЕ восстановлен
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == buyer.id)
        )).scalar_one()
    assert o.status == SHOP_ORDER_STATUS_DELIVERING
    assert u.balance_kopecks == 5000


async def test_ns_returned_refunded_marks_failed(db_factory, settings):
    """NS pre-check вернул REFUNDED — заказ failed + refund."""
    buyer, order = await _setup_paid_order(
        db_factory, balance_kopecks=10000, price_kopecks=5000,
    )
    pre_info = make_ns_info(
        OrderStatus.REFUNDED, status_message="provider returned funds",
    )
    ns = make_ns_mock(
        order_info_response=pre_info,
        create_order_raises=AssertionError("create НЕ должен"),
        pay_order_raises=AssertionError("pay НЕ должен"),
    )
    outcome = await deliver_shop_order_once(
        order.id, ns=ns, settings=settings,
    )
    assert outcome.failed is True

    async with db_factory() as s:
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == buyer.id)
        )).scalar_one()
    assert u.balance_kopecks == 10000


# ─── poll_shop_deliveries_once ────────────────────────────────────


async def test_poll_processes_multiple_paid_orders(db_factory, settings):
    """poll_shop_deliveries_once берёт max_per_run paid'ов и доставляет."""
    # Создадим 3 paid-заказа
    for i in range(3):
        async with db_factory() as s:
            buyer, _ = await get_or_create_user(s, telegram_user_id=100 + i)
            await apply_balance_change(
                s, user_id=buyer.id, change_kopecks=10000,
                reason="manual_topup",
            )
            apple_slug = make_group_slug(f"Brand{i}")
            await upsert_catalog_service(
                s, ns_service_id=10 + i, category_id=100 + i,
                category_name=f"X{i}", service_name=f"S{i}",
                base_name=f"Brand{i}", group_slug=apple_slug,
                ns_price_usd=5.0, rub_price_kopecks=5000,
                in_stock=10, fields_json=None,
            )
            await s.commit()
        async with db_factory() as s:
            await attempt_checkout_via_balance(
                s, user_id=buyer.id, ns_service_id=10 + i,
            )
            await s.commit()

    pay_resp = make_pay_resp(pins=[{"pin": "OK"}])
    ns = make_ns_mock(
        order_info_raises=_not_found(),
        create_order_response=SimpleNamespace(total_to_pay=5.0),
        pay_order_response=pay_resp,
    )
    metrics = await poll_shop_deliveries_once(
        ns=ns, settings=settings, max_per_run=10,
    )
    assert metrics["delivered"] == 3
    assert metrics["failed"] == 0
    assert metrics["pending"] == 0


async def test_poll_respects_max_per_run(db_factory, settings):
    """Если paid-заказов 10 а max=3 — берём только 3."""
    for i in range(10):
        async with db_factory() as s:
            buyer, _ = await get_or_create_user(s, telegram_user_id=100 + i)
            await apply_balance_change(
                s, user_id=buyer.id, change_kopecks=10000,
                reason="manual_topup",
            )
            await upsert_catalog_service(
                s, ns_service_id=10 + i, category_id=100 + i,
                category_name=f"X{i}", service_name=f"S{i}",
                base_name=f"B{i}", group_slug=make_group_slug(f"B{i}"),
                ns_price_usd=5.0, rub_price_kopecks=5000,
                in_stock=10, fields_json=None,
            )
            await s.commit()
        async with db_factory() as s:
            await attempt_checkout_via_balance(
                s, user_id=buyer.id, ns_service_id=10 + i,
            )
            await s.commit()

    pay_resp = make_pay_resp(pins=[{"pin": "OK"}])
    ns = make_ns_mock(
        order_info_raises=_not_found(),
        create_order_response=SimpleNamespace(total_to_pay=5.0),
        pay_order_response=pay_resp,
    )
    metrics = await poll_shop_deliveries_once(
        ns=ns, settings=settings, max_per_run=3,
    )
    assert metrics["checked"] == 3


async def test_poll_empty_returns_zeros(db_factory, settings):
    """Нет paid — метрики все 0."""
    ns = make_ns_mock()
    metrics = await poll_shop_deliveries_once(
        ns=ns, settings=settings, max_per_run=5,
    )
    assert metrics == {"checked": 0, "delivered": 0, "failed": 0, "pending": 0}
