"""
Тесты для обработчика кнопок алертов manual_hold (`TelegramBot._on_hold_click`).

Покрываем все три действия (`retry`, `done`, `show`) и edge-cases:
- неверный формат callback_data,
- пустой funpay_order_id,
- несуществующий заказ,
- неизвестное действие,
- идемпотентность `done` (повторный клик не ломает уже delivered),
- запрет retry для нерелевантных статусов,
- отсутствие подключённого `_order_retry`.

Идея: реальная in-memory SQLite (как в `test_setmarkup_command.py`) +
SimpleNamespace вместо aiogram CallbackQuery (мы не вызываем
aiogram-роутер, дёргаем `_on_hold_click` напрямую).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.alerts.bot import TelegramBot
from src.config import Settings
from src.db.repo import create_order, find_order_by_funpay_id
from src.db.session import init_db, session_factory


# ─────────────── общие фикстуры ───────────────


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1,
        ns_login="x",
        ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x",
        funpay_user_id=1,
    )


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Изолируем SQLite на tmp_path и сбрасываем фабрику сессий между тестами."""
    import src.db.session as sess_mod

    sess_mod._engine = None
    sess_mod._session_factory = None

    from src.config import Settings as _S, get_settings

    get_settings()
    orig_data = _S.data_path.fget
    monkeypatch.setattr(
        _S, "data_path", property(lambda self: tmp_path), raising=True
    )
    yield
    monkeypatch.setattr(_S, "data_path", property(orig_data), raising=True)
    sess_mod._engine = None
    sess_mod._session_factory = None


async def _seed_order(
    *,
    funpay_order_id: str = "FP1",
    status: str = "manual_hold",
    ns_custom_id: str | None = "NS-CUSTOM-1",
    error: str | None = None,
    description: str | None = "Тестовый лот, 1 шт.",
) -> None:
    """Создаёт заказ в БД с указанным статусом."""
    await init_db()
    async with session_factory()() as session:
        order = await create_order(
            session,
            funpay_order_id=funpay_order_id,
            funpay_lot_id=69300023,
            ns_service_id=42,
            buyer_username="buyer123",
            buyer_user_id=777,
            chat_id=888,
            quantity=1,
            funpay_price_rub=200.0,
            description=description,
        )
        order.status = status
        order.ns_custom_id = ns_custom_id
        if error is not None:
            order.error = error
        await session.commit()


def _make_cq(callback_data: str) -> SimpleNamespace:
    """
    Минимальный мок aiogram CallbackQuery.

    `cq.message.edit_text` / `cq.answer` — AsyncMock, чтобы проверить
    вызовы и аргументы.
    """
    message = SimpleNamespace(
        chat=SimpleNamespace(id=999),
        message_id=1,
        edit_text=AsyncMock(),
    )
    cq = SimpleNamespace(
        data=callback_data,
        message=message,
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=999),
    )
    return cq


def _bot(order_retry=None) -> TelegramBot:
    return TelegramBot(settings=_settings(), order_retry=order_retry)


# ─────────────── retry ───────────────


def test_retry_manual_hold_calls_order_retry_and_edits():
    """Кнопка Retry на manual_hold-заказе:
    дёргает _order_retry(funpay_order_id), отвечает callback'у и
    редактирует сообщение с результатом."""

    async def _run():
        await _seed_order(funpay_order_id="FP-RETRY", status="manual_hold")

        retry_mock = AsyncMock(return_value={"ok": True, "delivered": True})
        bot = _bot(order_retry=retry_mock)
        cq = _make_cq("hold:retry:FP-RETRY")

        await bot._on_hold_click(cq)

        retry_mock.assert_awaited_once_with("FP-RETRY")
        cq.answer.assert_awaited()
        first_call_args = cq.answer.await_args_list[0]
        text_arg = first_call_args.args[0] if first_call_args.args else first_call_args.kwargs.get("text")
        assert "Пробую доставить повторно" in str(text_arg)

        cq.message.edit_text.assert_awaited_once()
        edit_text = cq.message.edit_text.await_args.args[0]
        assert "Retry заказа" in edit_text
        assert "FP-RETRY" in edit_text
        assert "delivered" in edit_text

    asyncio.run(_run())


def test_retry_pins_ready_status_is_also_allowed():
    """Retry допустим и для pins_ready (когда дошло до выдачи, но
    что-то с FunPay-отправкой). Не должен говорить 'недоступен'."""

    async def _run():
        await _seed_order(funpay_order_id="FP-PINS", status="pins_ready")

        retry_mock = AsyncMock(return_value="ok")
        bot = _bot(order_retry=retry_mock)
        cq = _make_cq("hold:retry:FP-PINS")

        await bot._on_hold_click(cq)

        retry_mock.assert_awaited_once_with("FP-PINS")

    asyncio.run(_run())


def test_retry_wrong_status_rejected():
    """На статусе received Retry должен быть отклонён show_alert'ом,
    без вызова _order_retry."""

    async def _run():
        await _seed_order(funpay_order_id="FP-NEW", status="received")

        retry_mock = AsyncMock()
        bot = _bot(order_retry=retry_mock)
        cq = _make_cq("hold:retry:FP-NEW")

        await bot._on_hold_click(cq)

        retry_mock.assert_not_awaited()
        cq.answer.assert_awaited_once()
        kwargs = cq.answer.await_args.kwargs
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else kwargs.get("text")
        )
        assert kwargs.get("show_alert") is True
        assert "Retry недоступен" in str(text_arg)
        # сообщение не редактировали — кнопки на алерте остаются
        cq.message.edit_text.assert_not_awaited()

    asyncio.run(_run())


def test_retry_without_handler_returns_alert():
    """_order_retry = None — алерт 'Retry не подключён'."""

    async def _run():
        await _seed_order(funpay_order_id="FP-NR", status="manual_hold")

        bot = _bot(order_retry=None)
        cq = _make_cq("hold:retry:FP-NR")

        await bot._on_hold_click(cq)

        cq.answer.assert_awaited_once()
        kwargs = cq.answer.await_args.kwargs
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else kwargs.get("text")
        )
        assert "Retry не подключён" in str(text_arg)
        assert kwargs.get("show_alert") is True

    asyncio.run(_run())


# ─────────────── done (ручная выдача) ───────────────


def test_done_on_manual_hold_marks_delivered():
    """Кнопка 'Выдано вручную' переводит manual_hold → delivered,
    проставляет error='manual_delivered: ...' и НЕ запускает retry."""

    async def _run():
        await _seed_order(
            funpay_order_id="FP-DONE",
            status="manual_hold",
            error="ns timeout 600s",
        )

        retry_mock = AsyncMock()
        bot = _bot(order_retry=retry_mock)
        cq = _make_cq("hold:done:FP-DONE")

        await bot._on_hold_click(cq)

        retry_mock.assert_not_awaited()
        cq.answer.assert_awaited_once()
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else cq.answer.await_args.kwargs.get("text")
        )
        assert "Отмечено как выданное вручную" in str(text_arg)
        # сообщение НЕ редактируется (оператору не нужно тащить вверх простыню)
        cq.message.edit_text.assert_not_awaited()

        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, "FP-DONE")
            assert order is not None
            assert order.status == "delivered"
            assert order.error is not None and "manual_delivered" in order.error

    asyncio.run(_run())


def test_done_is_idempotent_on_already_delivered():
    """Повторный клик 'done' по уже delivered-заказу ничего не
    меняет (важно: если оператор тапнул дважды, мы не должны
    переписывать историю и не должны звать retry)."""

    async def _run():
        await _seed_order(
            funpay_order_id="FP-IDEMP",
            status="delivered",
            error="original error text",
        )

        retry_mock = AsyncMock()
        bot = _bot(order_retry=retry_mock)
        cq = _make_cq("hold:done:FP-IDEMP")

        await bot._on_hold_click(cq)

        retry_mock.assert_not_awaited()
        cq.answer.assert_awaited_once()
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else cq.answer.await_args.kwargs.get("text")
        )
        assert "Уже отмечен" in str(text_arg)

        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, "FP-IDEMP")
            assert order is not None
            assert order.status == "delivered"
            # error НЕ переписан manual_delivered'ом
            assert order.error == "original error text"

    asyncio.run(_run())


def test_done_on_wrong_status_is_rejected():
    """'Выдано вручную' разрешено ТОЛЬКО для manual_hold (и
    идемпотентно для delivered). Для received — alert."""

    async def _run():
        await _seed_order(funpay_order_id="FP-RX", status="received")

        bot = _bot(order_retry=AsyncMock())
        cq = _make_cq("hold:done:FP-RX")

        await bot._on_hold_click(cq)

        cq.answer.assert_awaited_once()
        kwargs = cq.answer.await_args.kwargs
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else kwargs.get("text")
        )
        assert "Доступно только для manual_hold" in str(text_arg)
        assert kwargs.get("show_alert") is True

        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, "FP-RX")
            assert order is not None
            assert order.status == "received"  # не изменился

    asyncio.run(_run())


# ─────────────── show (детали) ───────────────


def test_show_edits_message_with_order_details():
    """Кнопка 'Детали' редактирует сообщение, показывая все ключевые
    поля заказа: NS custom_id, статус, описание, ошибку."""

    async def _run():
        await _seed_order(
            funpay_order_id="FP-SHOW",
            status="manual_hold",
            ns_custom_id="NS-XYZ-9",
            description="Roblox 100 робуксов",
            error="превышен hard-timeout",
        )

        bot = _bot()
        cq = _make_cq("hold:show:FP-SHOW")

        await bot._on_hold_click(cq)

        cq.message.edit_text.assert_awaited_once()
        text = cq.message.edit_text.await_args.args[0]
        assert "FP-SHOW" in text
        assert "NS-XYZ-9" in text
        assert "manual_hold" in text
        assert "Roblox 100 робуксов" in text
        assert "превышен hard-timeout" in text

    asyncio.run(_run())


# ─────────────── невалидные callback_data ───────────────


def test_invalid_format_two_parts():
    """'hold:retry' (без funpay_order_id) — 'Неверный формат'."""

    async def _run():
        bot = _bot(order_retry=AsyncMock())
        cq = _make_cq("hold:retry")

        await bot._on_hold_click(cq)

        cq.answer.assert_awaited_once()
        kwargs = cq.answer.await_args.kwargs
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else kwargs.get("text")
        )
        assert "Неверный формат" in str(text_arg)
        assert kwargs.get("show_alert") is True

    asyncio.run(_run())


def test_empty_funpay_order_id():
    """'hold:retry:' — пустой order_id, отдельный алерт."""

    async def _run():
        bot = _bot(order_retry=AsyncMock())
        cq = _make_cq("hold:retry:")

        await bot._on_hold_click(cq)

        cq.answer.assert_awaited_once()
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else cq.answer.await_args.kwargs.get("text")
        )
        assert "Пустой order_id" in str(text_arg)

    asyncio.run(_run())


def test_order_not_in_db():
    """Несуществующий funpay_order_id — 'Заказ не найден в БД',
    без падения и без вызова _order_retry."""

    async def _run():
        await init_db()  # пустая БД

        retry_mock = AsyncMock()
        bot = _bot(order_retry=retry_mock)
        cq = _make_cq("hold:retry:NOPE-1")

        await bot._on_hold_click(cq)

        retry_mock.assert_not_awaited()
        cq.answer.assert_awaited_once()
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else cq.answer.await_args.kwargs.get("text")
        )
        assert "Заказ не найден" in str(text_arg)

    asyncio.run(_run())


def test_unknown_action():
    """'hold:explode:FP1' — 'Неизвестное действие: explode'.
    Защищает от случая, когда мы расширим список действий и
    клиент ткнёт старую кнопку."""

    async def _run():
        await _seed_order(funpay_order_id="FP-UNK", status="manual_hold")

        bot = _bot(order_retry=AsyncMock())
        cq = _make_cq("hold:explode:FP-UNK")

        await bot._on_hold_click(cq)

        cq.answer.assert_awaited_once()
        text_arg = (
            cq.answer.await_args.args[0]
            if cq.answer.await_args.args
            else cq.answer.await_args.kwargs.get("text")
        )
        assert "Неизвестное действие" in str(text_arg)
        assert "explode" in str(text_arg)

    asyncio.run(_run())


def test_funpay_order_id_with_colon_is_preserved():
    """funpay_order_id может содержать ':' (split с maxsplit=2):
    'hold:done:NS:WITH:COLON' → funpay_order_id == 'NS:WITH:COLON'."""

    async def _run():
        await _seed_order(funpay_order_id="NS:WITH:COLON", status="manual_hold")

        bot = _bot(order_retry=AsyncMock())
        cq = _make_cq("hold:done:NS:WITH:COLON")

        await bot._on_hold_click(cq)

        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, "NS:WITH:COLON")
            assert order is not None
            assert order.status == "delivered"

    asyncio.run(_run())
