"""
Дубли «спасибо за подтверждение заказа» при двойном confirm.

Сценарий из прода (источник дубля):
1) Покупатель оплачивает заказ → бот доставляет код.
2) Покупатель сам жмёт «Подтвердить» на FunPay → FunPay шлёт
   системку «Покупатель X подтвердил выполнение заказа #YYY...» →
   handler матчит kind=order_confirmed, помечает Order.confirmed_by=buyer,
   шлёт «спасибо за подтверждение» в чат FunPay.
3) Через 24+ часов FunPay шлёт ВТОРУЮ системку «Администратор Z
   подтвердил выполнение заказа #YYY...» (для уже подтверждённых
   заказов это бывает — отчётная цепочка FunPay) → handler матчит
   kind=order_confirmed_by_admin → mark_order_confirmed идемпотентен
   в БД (не меняет confirmed_by), НО handler всё равно шлёт reply
   второй раз. → ДУБЛЬ в чате с покупателем.

Защита должна быть в handler.py: если mark_order_confirmed вернул,
что подтверждение было НЕ первым (`was_first_confirmation=False`),
reply не отправляется.

Этот файл проверяет именно поведение handler.on_message —
с реальной in-memory БД, чтобы mark_order_confirmed реально отработал.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.chat.handler import ChatHandler
from src.config import Settings
from src.db import session as session_module
from src.db.models import Base, Order
from src.funpay.events import FunPayMessageEvent


# ─────────────────── Фикстуры ───────────────────


@pytest.fixture()
def in_memory_db(monkeypatch):
    """
    In-memory SQLite, подменяющий глобальный session_factory() в
    src.db.session. Нужно потому что handler.py делает
    `async with session_factory()() as session:` напрямую.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    monkeypatch.setattr(session_module, "_engine", engine)
    monkeypatch.setattr(session_module, "_session_factory", factory)

    yield factory

    asyncio.run(engine.dispose())


def _make_handler(my_username: str = "lol228822") -> tuple[ChatHandler, MagicMock, MagicMock]:
    fp = MagicMock()
    fp.my_username = my_username
    fp.account = SimpleNamespace(id=1, username=my_username)
    fp.send_message = AsyncMock()
    tg = MagicMock()
    tg.send = AsyncMock()
    settings = Settings(  # type: ignore[call-arg]
        ns_user_id=1,
        ns_login="x",
        ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x",
        funpay_user_id=1,
        chat_autogreeting_enabled=False,
    )
    return ChatHandler(fp, telegram=tg, settings=settings), fp, tg


async def _seed_delivered_order(factory, funpay_order_id: str) -> None:
    async with factory() as session:
        session.add(
            Order(
                funpay_order_id=funpay_order_id,
                funpay_lot_id=1,
                ns_service_id=42,
                buyer_username="Macan1467",
                quantity=1,
                funpay_price_rub=100.0,
                status="delivered",
            )
        )
        await session.commit()


def _buyer_confirm_event(order_id: str, *, chat_id: int = 104433092) -> FunPayMessageEvent:
    return FunPayMessageEvent(
        chat_id=chat_id,
        chat_username="Macan1467",
        author_id=None,
        author_username="Macan1467",
        text=(
            f"Покупатель Macan1467 подтвердил успешное выполнение заказа "
            f"#{order_id} и отправил деньги продавцу lol228822."
        ),
        is_my_message=False,
    )


def _admin_confirm_event(order_id: str, *, chat_id: int = 104433092) -> FunPayMessageEvent:
    return FunPayMessageEvent(
        chat_id=chat_id,
        chat_username="Macan1467",
        author_id=None,
        author_username="FunPay",
        text=(
            f"Администратор Palmira подтвердил успешное выполнение заказа "
            f"#{order_id} и отправил деньги продавцу lol228822."
        ),
        is_my_message=False,
    )


# ─────────────────── Тесты на дубль ───────────────────


def test_admin_confirm_after_buyer_confirm_does_not_duplicate_reply(in_memory_db):
    """
    Главный кейс. Сначала покупатель сам подтвердил → reply ушёл.
    Через 24ч пришло admin-подтверждение того же заказа →
    reply НЕ должен уйти второй раз.
    """
    asyncio.run(_seed_delivered_order(in_memory_db, "DUPL2025"))
    handler, fp, _ = _make_handler()

    asyncio.run(handler.on_message(_buyer_confirm_event("DUPL2025")))
    assert fp.send_message.call_count == 1, (
        "Первое buyer-подтверждение должно отправить reply"
    )

    asyncio.run(handler.on_message(_admin_confirm_event("DUPL2025")))
    assert fp.send_message.call_count == 1, (
        "ДУБЛЬ: admin-подтверждение для УЖЕ подтверждённого заказа "
        "не должно слать второй reply"
    )


def test_buyer_confirm_after_admin_confirm_does_not_duplicate_reply(in_memory_db):
    """
    Обратный порядок (саппорт подтвердил первым, покупатель — потом).
    Reply отправляется только на первое подтверждение.
    """
    asyncio.run(_seed_delivered_order(in_memory_db, "DUPL2026"))
    handler, fp, _ = _make_handler()

    asyncio.run(handler.on_message(_admin_confirm_event("DUPL2026")))
    assert fp.send_message.call_count == 1

    asyncio.run(handler.on_message(_buyer_confirm_event("DUPL2026")))
    assert fp.send_message.call_count == 1, (
        "ДУБЛЬ: buyer-подтверждение после admin не должно слать reply"
    )


def test_duplicate_buyer_confirm_does_not_duplicate_reply(in_memory_db):
    """
    Сам FunPay иногда шлёт системку дважды (бывает при сбоях UI).
    Reply должен уйти только один раз.
    """
    asyncio.run(_seed_delivered_order(in_memory_db, "DUPL2027"))
    handler, fp, _ = _make_handler()

    asyncio.run(handler.on_message(_buyer_confirm_event("DUPL2027")))
    asyncio.run(handler.on_message(_buyer_confirm_event("DUPL2027")))

    assert fp.send_message.call_count == 1, (
        "Дубликат системного сообщения от FunPay не должен слать второй reply"
    )


def test_confirmations_for_different_orders_send_separate_replies(in_memory_db):
    """
    Анти-регрессия: если подтверждаются ДВА разных заказа в одном
    чате (бывает: покупатель купил 2 раза, оба подтвердил), reply
    должен уйти на каждый.
    """
    asyncio.run(_seed_delivered_order(in_memory_db, "FIRST1"))
    asyncio.run(_seed_delivered_order(in_memory_db, "SECOND2"))
    handler, fp, _ = _make_handler()

    asyncio.run(handler.on_message(_buyer_confirm_event("FIRST1")))
    asyncio.run(handler.on_message(_buyer_confirm_event("SECOND2")))

    assert fp.send_message.call_count == 2, (
        "Два разных заказа — два разных reply"
    )


def test_confirm_for_unknown_order_still_sends_reply(in_memory_db):
    """
    Если заказа нет в БД (например, выдан до запуска бота),
    мы не можем определить «первое это подтверждение или нет» —
    отправляем reply (лучше отправить два раза для неизвестного,
    чем пропустить корректное подтверждение).
    """
    handler, fp, _ = _make_handler()

    asyncio.run(handler.on_message(_buyer_confirm_event("UNKNOWN9")))

    assert fp.send_message.call_count == 1, (
        "Неизвестный order_id всё равно должен получить reply"
    )
