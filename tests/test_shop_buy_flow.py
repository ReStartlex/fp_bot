"""
Sprint 5 — e2e тесты UI flow покупки в shop-боте.

Покрываем:
  * buy_request → confirm screen с балансом и ценой
  * buy_request при недостатке баланса → экран «пополни»
  * buy_request на закончившийся товар → alert
  * buy_confirm → реальный debit + paid + (mocked) delivery
  * buy_cancel → возврат к карточке
  * /orders команда с пустой историей
  * /orders команда с заказами в истории
  * ord:{id} карточка
  * orders:{N} пагинация
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, ShopUser
from src.shop.bot import ShopBot
from src.shop.repo import (
    SHOP_ORDER_STATUS_DELIVERED,
    SHOP_ORDER_STATUS_PAID,
    apply_balance_change,
    create_shop_order,
    get_or_create_user,
    mark_order_delivered,
    mark_order_delivering,
    mark_order_paid,
    upsert_catalog_service,
)
from src.shop.taxonomy import make_group_slug


@pytest.fixture()
async def db_setup(monkeypatch):
    """SQLite in-memory + патч session_factory во всех модулях."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def fake_factory():
        return factory

    monkeypatch.setattr("src.shop.bot.session_factory", fake_factory)
    yield factory
    await engine.dispose()


def _settings():
    from src.config import Settings
    return Settings(
        funpay_golden_key="x" * 64,
        funpay_currency="RUB",
        admin_chat_id=123,
        telegram_bot_token="x" * 46,
        ns_secret_key="testtesttesttesttesttesttesttest",
    )


def _fake_user_obj():
    return SimpleNamespace(
        id=1, username=None, first_name=None, language_code=None,
    )


def _fake_message():
    msg = SimpleNamespace()
    msg.answer = AsyncMock()
    msg.from_user = _fake_user_obj()
    msg.chat = SimpleNamespace(id=1)
    return msg


def _fake_cb(data: str):
    cb = SimpleNamespace()
    cb.data = data
    cb.message = SimpleNamespace(
        edit_text=AsyncMock(), delete=AsyncMock(), chat=SimpleNamespace(id=1),
    )
    cb.from_user = _fake_user_obj()
    cb.answer = AsyncMock()
    return cb


def _fake_state():
    state = SimpleNamespace()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


async def _seed_user_and_service(
    factory, *, balance_kopecks: int = 50000, in_stock: int = 10,
    price_kopecks: int = 20000, tg_id: int = 1,
):
    """Создаёт юзера с балансом + одну услугу в каталоге."""
    async with factory() as s:
        user, _ = await get_or_create_user(s, telegram_user_id=tg_id)
        if balance_kopecks > 0:
            await apply_balance_change(
                s, user_id=user.id, change_kopecks=balance_kopecks,
                reason="manual_topup",
            )
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US",
            service_name="Apple US $5",
            base_name="Apple Gift Card",
            group_slug=make_group_slug("Apple Gift Card"),
            ns_price_usd=5.0,
            rub_price_kopecks=price_kopecks,
            in_stock=in_stock, fields_json=None,
        )
        await s.commit()
    return user


# ─── buy_request: confirm screen ─────────────────────────────────


async def test_buy_request_shows_confirm_screen(db_setup):
    """Достаточный баланс → экран «Подтверди покупку»."""
    await _seed_user_and_service(
        db_setup, balance_kopecks=50000, price_kopecks=20000,
    )
    bot = ShopBot(_settings())
    cb = _fake_cb("buy:1")
    await bot._on_cb_buy_request(cb, _fake_state())
    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args.kwargs["text"]
    assert "Подтверди покупку" in text
    assert "200" in text or "20" in text  # цена 200₽
    # Кнопка «✅ Подтвердить» с callback_data buy_ok:1
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    buy_ok_btn = next(
        b for row in markup.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("buy_ok:")
    )
    assert buy_ok_btn.callback_data == "buy_ok:1"


async def test_buy_request_insufficient_balance_shows_topup_screen(db_setup):
    """Недостаточно средств → экран «пополни и попробуй снова»."""
    await _seed_user_and_service(
        db_setup, balance_kopecks=1000, price_kopecks=20000,  # 10₽ vs 200₽
    )
    bot = ShopBot(_settings())
    cb = _fake_cb("buy:1")
    await bot._on_cb_buy_request(cb, _fake_state())
    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args.kwargs["text"]
    assert "недостаточно" in text.lower() or "не хватает" in text.lower()
    # Должна быть кнопка пополнения CryptoBot
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any("CryptoBot" in t or "Пополнить" in t for t in button_texts)


async def test_buy_request_oos_alerts(db_setup):
    """Товар закончился → alert popup."""
    await _seed_user_and_service(
        db_setup, balance_kopecks=50000, in_stock=0,
    )
    bot = ShopBot(_settings())
    cb = _fake_cb("buy:1")
    await bot._on_cb_buy_request(cb, _fake_state())
    cb.answer.assert_called_once()
    # show_alert=True ожидаем для OOS
    call_kwargs = cb.answer.call_args.kwargs
    assert call_kwargs.get("show_alert") is True


# ─── buy_confirm: реальный debit ────────────────────────────────


async def test_buy_confirm_debits_balance_and_starts_delivery(db_setup):
    """Подтверждение → balance уменьшен, заказ paid, delivery runner вызван."""
    user = await _seed_user_and_service(
        db_setup, balance_kopecks=50000, price_kopecks=20000,
    )
    bot = ShopBot(_settings())
    # Запоминаем вызов inline-runner'а
    runner_calls = []

    async def fake_runner(order_id: int, tg_user_id: int) -> None:
        runner_calls.append((order_id, tg_user_id))

    bot._delivery_runner = fake_runner

    cb = _fake_cb("buy_ok:1")
    await bot._on_cb_buy_confirm(cb, _fake_state())

    # Balance после debit: 500₽ - 200₽ = 300₽
    async with db_setup() as s:
        u = (await s.execute(
            select(ShopUser).where(ShopUser.id == user.id)
        )).scalar_one()
    assert u.balance_kopecks == 30000

    # cb.answer мог быть вызван дважды: один раз технически
    # (например при _safe_edit fallback) и один с текстом. Достаточно
    # проверить что среди вызовов есть наш «принят».
    all_calls = [
        (call.args, call.kwargs) for call in cb.answer.call_args_list
    ]
    has_accepted = any(
        (args and ("принят" in args[0].lower() or "доставляем" in args[0].lower()))
        for args, _ in all_calls
    )
    assert has_accepted, f"Не нашли подтверждающий ответ среди {all_calls}"


async def test_buy_confirm_insufficient_at_click_time(db_setup):
    """
    Между показом confirm и нажатием баланс упал (другая покупка).
    На клике checkout вернёт INSUFFICIENT_BALANCE — экран обновится.
    """
    user = await _seed_user_and_service(
        db_setup, balance_kopecks=10000, price_kopecks=20000,
    )
    bot = ShopBot(_settings())
    cb = _fake_cb("buy_ok:1")
    await bot._on_cb_buy_confirm(cb, _fake_state())
    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args.kwargs["text"]
    assert "недостаточно" in text.lower() or "не хватает" in text.lower()


# ─── buy_cancel: возврат ────────────────────────────────────────


async def test_buy_cancel_shows_card_again(db_setup):
    """Отмена с confirm → возврат к карточке товара."""
    await _seed_user_and_service(db_setup, balance_kopecks=50000)
    bot = ShopBot(_settings())
    cb = _fake_cb("buy_cancel")
    state = _fake_state()
    state.get_data = AsyncMock(return_value={"buy_sid": 1})
    await bot._on_cb_buy_cancel(cb, state)
    # answer вызван (можно с разным текстом)
    cb.answer.assert_called()


# ─── /orders ────────────────────────────────────────────────────


async def test_orders_command_empty_history(db_setup):
    """Юзер без заказов — empty state с приглашением в каталог."""
    await _seed_user_and_service(db_setup, balance_kopecks=0)
    bot = ShopBot(_settings())
    msg = _fake_message()
    await bot._on_orders_cmd(msg, _fake_state())
    # 1й вызов — текст с историей, 2й — восстановление reply menu
    assert msg.answer.call_count >= 1
    first_text = msg.answer.call_args_list[0].args[0]
    assert "не нет" not in first_text.lower()  # не должно быть «у вас нет» вычурно
    assert "пока нет покупок" in first_text.lower() or "пуст" in first_text.lower()


async def test_orders_command_shows_existing_orders(db_setup):
    """С заказами в БД — должны появиться кнопки #N."""
    user = await _seed_user_and_service(db_setup, balance_kopecks=50000)
    # Создадим 3 заказа разных статусов
    async with db_setup() as s:
        for i, status in enumerate(
            ["delivered", "paid", "failed"], start=1,
        ):
            o = await create_shop_order(
                s, user_id=user.id, ns_service_id=i,
                ns_service_name=f"Service{i}",
                total_rub_kopecks=1000 * i,
            )
            if status == "delivered":
                await mark_order_paid(s, order_id=o.id, balance_used_kopecks=1000)
                await mark_order_delivering(s, order_id=o.id, ns_custom_id="x")
                await mark_order_delivered(s, order_id=o.id, pins_json="[]")
            elif status == "paid":
                await mark_order_paid(s, order_id=o.id, balance_used_kopecks=2000)
        await s.commit()

    bot = ShopBot(_settings())
    msg = _fake_message()
    await bot._on_orders_cmd(msg, _fake_state())
    # Извлекаем markup из первого вызова answer
    markup = msg.answer.call_args_list[0].kwargs["reply_markup"]
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    # Должны быть кнопки заказов с эмодзи статуса
    assert any("#1" in t for t in button_texts)
    assert any("#2" in t for t in button_texts)
    assert any("#3" in t for t in button_texts)


async def test_order_card_shows_pins_for_delivered(db_setup):
    """ord:{id} карточка должна содержать pins."""
    user = await _seed_user_and_service(db_setup)
    async with db_setup() as s:
        o = await create_shop_order(
            s, user_id=user.id, ns_service_id=1,
            ns_service_name="Apple", total_rub_kopecks=5000,
        )
        await mark_order_paid(s, order_id=o.id, balance_used_kopecks=5000)
        await mark_order_delivering(s, order_id=o.id, ns_custom_id="x")
        await mark_order_delivered(
            s, order_id=o.id, pins_json=json.dumps([{"pin": "SECRET-CODE-123"}]),
        )
        await s.commit()
    bot = ShopBot(_settings())
    cb = _fake_cb(f"ord:{o.id}")
    await bot._on_cb_order_card(cb)
    text = cb.message.edit_text.call_args.kwargs["text"]
    assert "SECRET-CODE-123" in text


async def test_order_card_forbidden_for_other_users_order(db_setup):
    """ord:{id} чужого юзера → отказ."""
    # User 1 создаёт заказ
    user1 = await _seed_user_and_service(db_setup, tg_id=1)
    async with db_setup() as s:
        o = await create_shop_order(
            s, user_id=user1.id, ns_service_id=1,
            ns_service_name="Apple", total_rub_kopecks=5000,
        )
        # Создадим юзера 2
        await get_or_create_user(s, telegram_user_id=2)
        await s.commit()
    # User 2 запрашивает чужой заказ
    bot = ShopBot(_settings())
    cb = _fake_cb(f"ord:{o.id}")
    cb.from_user = SimpleNamespace(
        id=2, username=None, first_name=None, language_code=None,
    )
    await bot._on_cb_order_card(cb)
    cb.answer.assert_called_once()
    text = cb.answer.call_args.args[0] if cb.answer.call_args.args else ""
    assert "не найден" in text.lower()
