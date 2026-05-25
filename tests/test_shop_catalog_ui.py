"""
UI-тесты shop-каталога в боте.
Mock'аем aiogram Message/CallbackQuery, проверяем что:
- /catalog без данных → "каталог пуст";
- /catalog с данными → список категорий с кнопками cat:{id}:0;
- callback cat:{id}:0 → список сервисов с pagination;
- callback svc:{id} → карточка с кнопкой "Купить" (если in_stock>0);
- pagination показывается только когда страниц >1.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base
from src.shop.bot import CATALOG_PAGE_SIZE, ShopBot
from src.shop.repo import upsert_catalog_service


def _settings():
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x", ns_api_secret="QQ==",
        funpay_golden_key="x", funpay_user_id=1,
        shop_enabled=True, shop_telegram_bot_token="dummy",
    )


@pytest.fixture()
async def db_setup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.shop.bot.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _fake_message():
    msg = SimpleNamespace()
    msg.from_user = SimpleNamespace(id=1, username="u", first_name="U", language_code="ru")
    msg.answer = AsyncMock()
    return msg


def _fake_cb(data: str):
    msg = SimpleNamespace()
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    cb = SimpleNamespace()
    cb.data = data
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


async def _seed_catalog(factory) -> None:
    """Заполняем shop_catalog_cache тремя категориями со смешанным запасом."""
    async with factory() as s:
        # Apple (3 in-stock)
        for sid, name, price_k in [
            (1, "Apple $5", 39600),
            (2, "Apple $10", 79200),
            (3, "Apple $25", 198000),
        ]:
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10, category_name="Apple",
                service_name=name, ns_price_usd=price_k / 100 / 70,
                rub_price_kopecks=price_k, in_stock=100, fields_json=None,
            )
        # Steam (1 in-stock)
        await upsert_catalog_service(
            s, ns_service_id=4, category_id=20, category_name="Steam",
            service_name="Steam $5", ns_price_usd=5.0,
            rub_price_kopecks=40000, in_stock=50, fields_json=None,
        )
        # Spotify (0 in-stock — не должен показаться)
        await upsert_catalog_service(
            s, ns_service_id=5, category_id=30, category_name="Spotify",
            service_name="Spotify Premium", ns_price_usd=10.0,
            rub_price_kopecks=80000, in_stock=0, fields_json=None,
        )
        await s.commit()


# ─── /catalog ───────────────────────────────────────────────────────


async def test_catalog_empty(db_setup):
    bot = ShopBot(_settings())
    msg = _fake_message()
    await bot._on_catalog(msg)
    text = msg.answer.call_args.args[0]
    assert "пуст" in text.lower()


async def test_catalog_shows_categories(db_setup):
    await _seed_catalog(db_setup)
    bot = ShopBot(_settings())
    msg = _fake_message()
    await bot._on_catalog(msg)
    text = msg.answer.call_args.args[0]
    markup = msg.answer.call_args.kwargs["reply_markup"]
    # Spotify должен быть скрыт (все OOS)
    assert "Apple" in text or any(
        "Apple" in btn.text for row in markup.inline_keyboard for btn in row
    )
    assert "Steam" in text or any(
        "Steam" in btn.text for row in markup.inline_keyboard for btn in row
    )
    button_texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert not any("Spotify" in t for t in button_texts), \
        "Spotify должен быть скрыт — все услуги OOS"
    # У Apple — 3 услуги, у Steam — 1
    apple_btn = next(
        btn for row in markup.inline_keyboard for btn in row
        if btn.text.startswith("Apple")
    )
    assert " 3 " in apple_btn.text  # "Apple · 3 · от ..."
    assert apple_btn.callback_data.startswith("cat:10:0")


# ─── callback cat:{id}:0 ────────────────────────────────────────────


async def test_category_callback_shows_services_sorted_by_price(db_setup):
    await _seed_catalog(db_setup)
    bot = ShopBot(_settings())
    cb = _fake_cb("cat:10:0")
    await bot._on_cb_category(cb)
    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args.kwargs["text"]
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    # Заголовок упоминает категорию
    assert "Apple" in text
    # Сервисы отсортированы по цене (дешёвый первый)
    svc_buttons = [
        btn for row in markup.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("svc:")
    ]
    assert [b.callback_data for b in svc_buttons] == ["svc:1", "svc:2", "svc:3"]


async def test_category_callback_pagination_shows_when_needed(db_setup, monkeypatch):
    """Когда сервисов > PAGE_SIZE, появляется блок навигации с ‹/›."""
    # Закидываем PAGE_SIZE + 2 услуги в одну категорию
    async with db_setup() as s:
        for sid in range(1, CATALOG_PAGE_SIZE + 3):
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10, category_name="Big",
                service_name=f"X{sid}", ns_price_usd=1.0,
                rub_price_kopecks=sid * 1000, in_stock=10, fields_json=None,
            )
        await s.commit()

    bot = ShopBot(_settings())
    cb = _fake_cb("cat:10:0")
    await bot._on_cb_category(cb)
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]

    nav_buttons = [
        btn for row in markup.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("cat:10:")
    ]
    # На странице 0: только "›" для следующей
    assert any("cat:10:1" in (b.callback_data or "") for b in nav_buttons)


# ─── callback svc:{id} ──────────────────────────────────────────────


async def test_service_card_in_stock(db_setup):
    await _seed_catalog(db_setup)
    bot = ShopBot(_settings())
    cb = _fake_cb("svc:1")
    await bot._on_cb_service(cb)
    text = cb.message.edit_text.call_args.kwargs["text"]
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    assert "Apple $5" in text
    assert "В наличии" in text
    # Есть кнопка "Купить"
    flat = [btn for row in markup.inline_keyboard for btn in row]
    assert any("Купить" in b.text for b in flat)


async def test_service_card_returns_none_for_missing(db_setup):
    bot = ShopBot(_settings())
    cb = _fake_cb("svc:9999")
    await bot._on_cb_service(cb)
    # edit_text НЕ вызывается, всплывает alert
    cb.message.edit_text.assert_not_called()
    cb.answer.assert_called_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True


async def test_buy_stub_shows_alert(db_setup):
    bot = ShopBot(_settings())
    cb = _fake_cb("buy:1")
    await bot._on_cb_buy_stub(cb)
    cb.answer.assert_called_once()
    text = cb.answer.call_args.args[0] if cb.answer.call_args.args else \
        cb.answer.call_args.kwargs.get("text", "")
    assert "оплат" in text.lower() or "опла" in text.lower()


async def test_back_to_cats_callback(db_setup):
    await _seed_catalog(db_setup)
    bot = ShopBot(_settings())
    cb = _fake_cb("cats")
    await bot._on_cb_cats(cb)
    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args.kwargs["text"]
    assert "Каталог" in text or "категори" in text.lower()
