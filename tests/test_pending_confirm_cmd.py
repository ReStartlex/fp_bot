"""
Тесты команды /pending_confirm.

Покрывают:
- _parse_hours_arg: default, кастомное число, clamping, мусор;
- _collect_pending_confirm: интеграция с БД (in-memory SQLite),
  фильтр по cutoff, NULL confirmed_at, статус;
- _do_pending_confirm: формат ответа в Telegram (есть готовый блок
  «#ID, #ID, #ID» для копирования в саппорт; есть имя покупателя).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.alerts.bot import TelegramBot, _parse_hours_arg
from src.db.models import Base, Order


# ─────────────────────── _parse_hours_arg ───────────────────────


def test_parse_hours_default_when_no_arg():
    assert _parse_hours_arg("/pending_confirm", default=24) == 24


def test_parse_hours_default_when_none():
    assert _parse_hours_arg(None, default=24) == 24


def test_parse_hours_default_when_empty():
    assert _parse_hours_arg("", default=24) == 24


def test_parse_hours_custom_value():
    assert _parse_hours_arg("/pending_confirm 12", default=24) == 12
    assert _parse_hours_arg("/pending_confirm 48", default=24) == 48


def test_parse_hours_clamps_too_small():
    """1ч — минимум, защита от 0/отрицательных."""
    assert _parse_hours_arg("/pending_confirm 0", default=24) == 1
    assert _parse_hours_arg("/pending_confirm -5", default=24) == 1


def test_parse_hours_clamps_too_large():
    """168ч (1 неделя) — максимум, защита от случайных 100000."""
    assert _parse_hours_arg("/pending_confirm 999", default=24) == 168
    assert _parse_hours_arg("/pending_confirm 100000", default=24) == 168


def test_parse_hours_ignores_garbage():
    """Не число → default."""
    assert _parse_hours_arg("/pending_confirm abc", default=24) == 24
    assert _parse_hours_arg("/pending_confirm 12hours", default=24) == 24
    assert _parse_hours_arg("/pending_confirm 12.5", default=24) == 24


# ───────────── _collect_pending_confirm + _do_pending_confirm ─────────────


@pytest.fixture()
async def db_with_orders(monkeypatch):
    """In-memory БД с тремя заказами для проверки фильтрации/формата."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.utcnow()
    async with factory() as session:
        # Подходит: delivered, 30ч, не подтверждён
        old_unconfirmed = Order(
            funpay_order_id="OLD12345",
            funpay_lot_id=1,
            ns_service_id=42,
            buyer_username="Macan1467",
            quantity=1,
            funpay_price_rub=100.0,
            status="delivered",
        )
        session.add(old_unconfirmed)
        await session.flush()
        old_unconfirmed.updated_at = now - timedelta(hours=30)

        # Не подходит: подтверждён
        confirmed = Order(
            funpay_order_id="DONE1234",
            funpay_lot_id=1,
            ns_service_id=42,
            buyer_username="JohnDoe",
            quantity=1,
            funpay_price_rub=100.0,
            status="delivered",
            confirmed_at=now,
            confirmed_by="buyer",
        )
        session.add(confirmed)
        await session.flush()
        confirmed.updated_at = now - timedelta(hours=30)

        # Не подходит: свежий (2ч)
        fresh = Order(
            funpay_order_id="FRESH123",
            funpay_lot_id=1,
            ns_service_id=42,
            buyer_username="Alice",
            quantity=1,
            funpay_price_rub=100.0,
            status="delivered",
        )
        session.add(fresh)
        await session.flush()
        fresh.updated_at = now - timedelta(hours=2)

        await session.commit()

    monkeypatch.setattr("src.alerts.bot.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _make_bot_with_owner_check_disabled() -> TelegramBot:
    """Создаём TelegramBot, минуя aiogram-инициализацию (нам нужны только
    _do_pending_confirm и _collect_pending_confirm)."""
    bot = TelegramBot.__new__(TelegramBot)
    bot._send_view = AsyncMock()  # type: ignore[attr-defined]
    return bot


@pytest.mark.asyncio
async def test_collect_pending_confirm_filters_correctly(db_with_orders):
    bot = _make_bot_with_owner_check_disabled()
    orders = await bot._collect_pending_confirm(hours=24)
    assert [o.funpay_order_id for o in orders] == ["OLD12345"]


@pytest.mark.asyncio
async def test_do_pending_confirm_renders_copyable_list(db_with_orders):
    """
    Главный кейс: ответ содержит готовый блок для копирования в саппорт
    + имя покупателя + возраст в часах.
    """
    bot = _make_bot_with_owner_check_disabled()
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=12345)
    msg.text = "/pending_confirm"

    await bot._do_pending_confirm(msg)

    bot._send_view.assert_called_once()
    call_args = bot._send_view.call_args
    chat_id = call_args.args[0]
    text = call_args.args[1]

    assert chat_id == 12345
    # Должен быть order_id
    assert "#OLD12345" in text
    # Имя покупателя для контекста саппорта
    assert "Macan1467" in text
    # Готовый блок для копирования (одна строка с #ID через запятую)
    assert "#OLD12345" in text and "Скопировать" in text
    # НЕ должно быть подтверждённого или свежего заказа
    assert "DONE1234" not in text
    assert "FRESH123" not in text


@pytest.mark.asyncio
async def test_do_pending_confirm_empty_state(db_with_orders):
    """Если ничего не нашлось — приятное сообщение, не пустая простыня."""
    bot = _make_bot_with_owner_check_disabled()
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=12345)
    # Cutoff 100ч: даже 30-часовой заказ не попадёт
    msg.text = "/pending_confirm 100"

    await bot._do_pending_confirm(msg)

    text = bot._send_view.call_args.args[1]
    assert "Нет заказов" in text or "не найдено" in text.lower()
    assert "OLD12345" not in text


@pytest.mark.asyncio
async def test_do_pending_confirm_custom_hours(db_with_orders):
    """С /pending_confirm 1 — свежий тоже попадёт (2ч > 1ч)."""
    bot = _make_bot_with_owner_check_disabled()
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=12345)
    msg.text = "/pending_confirm 1"

    await bot._do_pending_confirm(msg)

    text = bot._send_view.call_args.args[1]
    assert "#OLD12345" in text
    assert "#FRESH123" in text
    assert "#DONE1234" not in text  # подтверждённый никогда не показывается
