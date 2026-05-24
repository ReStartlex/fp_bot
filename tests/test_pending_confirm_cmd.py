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

from src.alerts.bot import (
    TelegramBot,
    _parse_hours_arg,
    _split_ids_to_copy_chunks,
    _split_lines_to_chunks,
)
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

    Ответ теперь шлётся МИНИМУМ двумя сообщениями (список + copy-block),
    чтобы не выйти за Telegram 4096-char limit на больших списках.
    """
    bot = _make_bot_with_owner_check_disabled()
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=12345)
    msg.text = "/pending_confirm"

    await bot._do_pending_confirm(msg)

    # Минимум 2 сообщения: список + copy-block
    assert bot._send_view.call_count >= 2

    # Все сообщения в нужный chat_id
    for call in bot._send_view.call_args_list:
        assert call.args[0] == 12345

    # Объединённый текст всех сообщений
    all_text = "\n".join(call.args[1] for call in bot._send_view.call_args_list)
    assert "#OLD12345" in all_text
    assert "Macan1467" in all_text  # имя покупателя для саппорта
    assert "Скопировать" in all_text  # есть метка copy-block
    assert "DONE1234" not in all_text  # подтверждённый не показан
    assert "FRESH123" not in all_text  # свежий не показан


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

    # Должно быть как минимум 2 send_view вызова: список + copy-block.
    assert bot._send_view.call_count >= 2
    all_texts = " ".join(call.args[1] for call in bot._send_view.call_args_list)
    assert "#OLD12345" in all_texts
    assert "#FRESH123" in all_texts
    assert "#DONE1234" not in all_texts  # подтверждённый никогда не показывается


# ───────────────────── chunking helpers ─────────────────────


def test_split_lines_to_chunks_small_fits_in_one():
    chunks = _split_lines_to_chunks(
        header_lines=["HEADER"],
        body_lines=["line1", "line2", "line3"],
        max_chars=1000,
    )
    assert len(chunks) == 1
    assert "HEADER" in chunks[0]
    assert "line1" in chunks[0]
    assert "line3" in chunks[0]


def test_split_lines_to_chunks_splits_when_over_limit():
    """500 строк по 60 char (~30KB) с лимитом 3500 → ~10 чанков."""
    body = [f"line {i:03d} " + ("x" * 50) for i in range(500)]
    chunks = _split_lines_to_chunks(
        header_lines=["HDR"],
        body_lines=body,
        max_chars=3500,
    )
    assert len(chunks) >= 2, "Должно разрезаться на несколько чанков"
    # Каждый чанк ≤ ожидаемого + 1 заголовок (header ≤ 100 chars)
    for c in chunks:
        assert len(c) <= 3700, f"Чанк превысил лимит: {len(c)} chars"
    # Все строки присутствуют в каком-то чанке
    all_text = "\n".join(chunks)
    for i in range(500):
        assert f"line {i:03d}" in all_text, f"Потеряна строка {i}"


def test_split_lines_to_chunks_every_chunk_starts_with_header():
    """Юзер не должен теряться: каждое сообщение должно сразу
    показывать о чём оно."""
    body = ["x" * 100 for _ in range(100)]
    chunks = _split_lines_to_chunks(
        header_lines=["📋 SUPPORT LIST"],
        body_lines=body,
        max_chars=500,
    )
    assert len(chunks) > 1
    for c in chunks:
        assert c.startswith("📋 SUPPORT LIST"), (
            f"Чанк не начинается с заголовка: {c[:50]!r}"
        )


def test_split_lines_to_chunks_empty_body():
    chunks = _split_lines_to_chunks(
        header_lines=["only header"],
        body_lines=[],
    )
    assert chunks == ["only header"]


def test_split_ids_to_copy_chunks_small():
    chunks = _split_ids_to_copy_chunks(["AAA111", "BBB222", "CCC333"])
    assert chunks == ["#AAA111, #BBB222, #CCC333"]


def test_split_ids_to_copy_chunks_large():
    """500 заказов с лимитом 3500 → несколько чанков, без потерь."""
    ids = [f"ORD{i:05d}" for i in range(500)]
    chunks = _split_ids_to_copy_chunks(ids, max_chars=3500)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 3500
    joined = ", ".join(chunks)
    for i in range(500):
        assert f"#ORD{i:05d}" in joined, f"Потерян ORD{i:05d}"


def test_split_ids_to_copy_chunks_no_trailing_separator():
    chunks = _split_ids_to_copy_chunks(["A1", "B2"])
    for c in chunks:
        assert not c.endswith(", "), f"Trailing separator: {c!r}"
        assert not c.startswith(", "), f"Leading separator: {c!r}"


def test_split_ids_to_copy_chunks_empty():
    assert _split_ids_to_copy_chunks([]) == []


# ───────────────────── chunking integration ─────────────────────


@pytest.fixture()
async def db_with_many_orders(monkeypatch):
    """БД с 200 delivered-заказами старше 24ч — для проверки чанкинга."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    long_ago = datetime.utcnow() - timedelta(hours=30)
    async with factory() as session:
        for i in range(200):
            order = Order(
                funpay_order_id=f"ORD{i:05d}",
                funpay_lot_id=1,
                ns_service_id=42,
                buyer_username=f"BuyerWithLongUsername_{i:03d}",
                quantity=1,
                funpay_price_rub=100.0,
                status="delivered",
            )
            session.add(order)
            await session.flush()
            order.updated_at = long_ago - timedelta(seconds=i)
        await session.commit()

    monkeypatch.setattr("src.alerts.bot.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_do_pending_confirm_handles_200_orders_without_overflow(
    db_with_many_orders,
):
    """
    Регрессия на bug 24.05.2026:
      «Telegram server says - Bad Request: message is too long»
    200 заказов по ~60 chars = ~12KB → должны разъехаться на чанки.
    """
    bot = _make_bot_with_owner_check_disabled()
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=12345)
    msg.text = "/pending_confirm"

    await bot._do_pending_confirm(msg)

    # Проверка №1: НИ ОДНО отправленное сообщение не превысило 4096 chars.
    for call in bot._send_view.call_args_list:
        text = call.args[1]
        assert len(text) <= 4096, (
            f"Сообщение {len(text)} chars превышает Telegram limit 4096. "
            f"Превью: {text[:200]!r}"
        )

    # Проверка №2: все 200 order_id присутствуют где-то в выводе.
    all_text = " ".join(c.args[1] for c in bot._send_view.call_args_list)
    for i in range(200):
        assert f"ORD{i:05d}" in all_text, f"Потерян #ORD{i:05d} в выводе"

    # Проверка №3: должно быть несколько сообщений (точно > 1).
    assert bot._send_view.call_count >= 2, (
        "Ожидалось разбиение на несколько сообщений из-за объёма"
    )
