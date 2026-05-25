"""
UI-тесты shop-каталога в боте.
Mock'аем aiogram Message/CallbackQuery, проверяем что:
- /catalog без данных → "каталог пуст";
- /catalog показывает ГРУППЫ (свёрнутые региональные варианты);
- callback grp:{slug} → drill-down: если 1 кат — сразу сервисы, если >1 — список регионов;
- callback cat:{id}:0 → список сервисов с pagination;
- callback svc:{id} → карточка с кнопкой "Купить" (если in_stock>0);
- pagination показывается только когда страниц >1;
- /search query → результаты, callback srh пагинируется.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base
from src.shop.bot import ShopBot
from src.shop.keyboards import SERVICES_PAGE_SIZE as CATALOG_PAGE_SIZE
from src.shop.repo import upsert_catalog_service
from src.shop.taxonomy import make_group_slug


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
    msg.chat = SimpleNamespace(id=1)
    msg.text = None
    return msg


def _fake_cb(data: str):
    msg = SimpleNamespace()
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    msg.chat = SimpleNamespace(id=1)
    cb = SimpleNamespace()
    cb.data = data
    cb.message = msg
    cb.from_user = SimpleNamespace(id=1)
    cb.answer = AsyncMock()
    return cb


def _fake_state():
    """Минимальный fake aiogram FSMContext: достаточно методов clear()/set_state()."""
    state = SimpleNamespace()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()
    state.get_state = AsyncMock(return_value=None)
    return state


async def _seed_catalog(factory) -> None:
    """Заполняем shop_catalog_cache: Apple (1 кат, 3 услуги), Steam (1 кат, 1 услуга),
    Spotify (1 кат, 0 in-stock — должен скрыться)."""
    apple_slug = make_group_slug("Apple")
    steam_slug = make_group_slug("Steam")
    spot_slug = make_group_slug("Spotify")
    async with factory() as s:
        for sid, name, price_k in [
            (1, "Apple $5", 39600),
            (2, "Apple $10", 79200),
            (3, "Apple $25", 198000),
        ]:
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10, category_name="Apple",
                service_name=name, ns_price_usd=price_k / 100 / 70,
                rub_price_kopecks=price_k, in_stock=100, fields_json=None,
                base_name="Apple", group_slug=apple_slug,
            )
        await upsert_catalog_service(
            s, ns_service_id=4, category_id=20, category_name="Steam",
            service_name="Steam $5", ns_price_usd=5.0,
            rub_price_kopecks=40000, in_stock=50, fields_json=None,
            base_name="Steam", group_slug=steam_slug,
        )
        await upsert_catalog_service(
            s, ns_service_id=5, category_id=30, category_name="Spotify",
            service_name="Spotify Premium", ns_price_usd=10.0,
            rub_price_kopecks=80000, in_stock=0, fields_json=None,
            base_name="Spotify", group_slug=spot_slug,
        )
        await s.commit()


# ─── /catalog ───────────────────────────────────────────────────────


async def test_catalog_empty(db_setup):
    bot = ShopBot(_settings())
    msg = _fake_message()
    await bot._on_catalog_cmd(msg, _fake_state())
    text = msg.answer.call_args.args[0]
    assert "пуст" in text.lower()


async def test_catalog_shows_groups(db_setup):
    """В верхнем меню — группы по base_name, не плоские категории."""
    await _seed_catalog(db_setup)
    bot = ShopBot(_settings())
    msg = _fake_message()
    await bot._on_catalog_cmd(msg, _fake_state())
    markup = msg.answer.call_args.kwargs["reply_markup"]
    button_texts = [btn.text for row in markup.inline_keyboard for btn in row]
    # Spotify скрыт (OOS)
    assert not any("Spotify" in t for t in button_texts)
    # Apple и Steam — две группы. После Sprint 4 кнопки начинаются с
    # brand-эмодзи (🍎 Apple…, 🎮 Steam…) и featured-бейджа (🔥/⭐/💎)
    # для топ-3 — поэтому ищем «Apple» внутри текста, а не префиксом.
    apple_btn = next(
        btn for row in markup.inline_keyboard for btn in row
        if "Apple" in btn.text
    )
    # У Apple одна категория, поэтому «регион»/«вариант» не упоминаем
    assert "вариант" not in apple_btn.text.lower()
    assert "регион" not in apple_btn.text.lower()
    # Brand-эмодзи 🍎 должен быть рядом с названием
    assert "🍎" in apple_btn.text
    # Callback ведёт на drill-down группы
    assert apple_btn.callback_data.startswith("grp:")


async def test_catalog_with_regional_variants_groups_them(db_setup):
    """Apple Gift Card | US, | EU, | UK → один пункт в каталоге с N регионами."""
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_setup() as s:
        for sid, region, price in [(1, "US", 40000), (2, "EU", 44000), (3, "UK", 48000)]:
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10 + sid,
                category_name=f"Apple Gift Card | {region}",
                service_name=f"Apple {region}",
                base_name="Apple Gift Card", group_slug=apple_slug,
                ns_price_usd=5.0, rub_price_kopecks=price,
                in_stock=10, fields_json=None,
            )
        await s.commit()

    bot = ShopBot(_settings())
    msg = _fake_message()
    await bot._on_catalog_cmd(msg, _fake_state())
    markup = msg.answer.call_args.kwargs["reply_markup"]
    apple_btn = next(
        btn for row in markup.inline_keyboard for btn in row
        if "Apple Gift Card" in btn.text
    )
    # 3 региональных category_id → должно появиться "3 региона"
    assert "3" in apple_btn.text
    assert "регион" in apple_btn.text.lower()
    # И минимальная цена — 400₽
    assert "400" in apple_btn.text
    assert apple_btn.callback_data == f"grp:{apple_slug}:0"


async def test_group_drill_down_with_multiple_variants(db_setup):
    """grp:{slug} с >1 категории → список регионов."""
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_setup() as s:
        for sid, region in [(1, "US"), (2, "EU")]:
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10 + sid,
                category_name=f"Apple Gift Card | {region}",
                service_name=f"Apple {region}",
                base_name="Apple Gift Card", group_slug=apple_slug,
                ns_price_usd=5.0, rub_price_kopecks=40000,
                in_stock=10, fields_json=None,
            )
        await s.commit()

    bot = ShopBot(_settings())
    cb = _fake_cb(f"grp:{apple_slug}:0")
    await bot._on_cb_group(cb)
    cb.message.edit_text.assert_called_once()
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    cat_buttons = [
        btn for row in markup.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("cat:")
    ]
    assert len(cat_buttons) == 2  # EU, US


async def test_group_drill_down_single_variant_skips_to_services(db_setup):
    """grp:{slug} с 1 категорией внутри — сразу показывает сервисы."""
    await _seed_catalog(db_setup)
    apple_slug = make_group_slug("Apple")
    bot = ShopBot(_settings())
    cb = _fake_cb(f"grp:{apple_slug}:0")
    await bot._on_cb_group(cb)
    cb.message.edit_text.assert_called_once()
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    # На экране — сервисы Apple, кнопки svc:N
    svc_buttons = [
        btn for row in markup.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("svc:")
    ]
    assert len(svc_buttons) == 3


async def test_singleton_group_back_button_goes_to_catalog_not_self(db_setup):
    """
    БАГ-РЕГРЕССИЯ: если в группе только 1 категория, кнопка «назад»
    должна вести в каталог, а не на ту же группу (где drill-down вернёт
    тот же экран — Telegram игнорит, юзер думает что бот сломан).
    """
    await _seed_catalog(db_setup)  # Apple — singleton (1 category_id)
    bot = ShopBot(_settings())
    # Эмулируем переход «cat:10:0» (категория Apple — единственная в группе)
    cb = _fake_cb("cat:10:0")
    await bot._on_cb_category(cb)
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    back_btns = [
        b for row in markup.inline_keyboard for b in row
        if b.callback_data and (
            b.callback_data.startswith("grp:") or b.callback_data.startswith("cats")
        )
    ]
    # Не должно быть кнопок grp:{slug}:0 — все обратные ссылки → cats:0
    assert all("cats" in (b.callback_data or "") for b in back_btns), \
        f"Singleton-группа дала grp-back, а должна cats: {[b.callback_data for b in back_btns]}"


async def test_multivariant_group_back_button_returns_to_variants(db_setup):
    """Для группы с >1 категории — back ведёт на группу (region picker)."""
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_setup() as s:
        for sid, region, price in [(1, "US", 40000), (2, "EU", 44000)]:
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10 + sid,
                category_name=f"Apple Gift Card | {region}",
                service_name=f"Apple {region}",
                base_name="Apple Gift Card", group_slug=apple_slug,
                ns_price_usd=5.0, rub_price_kopecks=price,
                in_stock=10, fields_json=None,
            )
        await s.commit()

    bot = ShopBot(_settings())
    cb = _fake_cb("cat:11:0")  # US
    await bot._on_cb_category(cb)
    markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
    back_btns = [
        b for row in markup.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("grp:")
    ]
    assert back_btns, "Multi-variant группа должна давать кнопку grp:..."


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
    cat_slug = make_group_slug("Big")
    async with db_setup() as s:
        for sid in range(1, CATALOG_PAGE_SIZE + 3):
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10, category_name="Big",
                service_name=f"X{sid}", ns_price_usd=1.0,
                rub_price_kopecks=sid * 1000, in_stock=10, fields_json=None,
                base_name="Big", group_slug=cat_slug,
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
    cb = _fake_cb("cats:0")
    await bot._on_cb_cats(cb)
    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args.kwargs["text"]
    assert "Каталог" in text or "категори" in text.lower()


# ─── /search ────────────────────────────────────────────────────────


def _fake_command(args: str | None):
    """Минимальный CommandObject для message.command."""
    return SimpleNamespace(args=args)


async def test_search_empty_query_enters_fsm(db_setup):
    """/search без аргументов — переход в FSM-flow, бот спрашивает фразу."""
    bot = ShopBot(_settings())
    msg = _fake_message()
    state = _fake_state()
    await bot._on_search_cmd(msg, _fake_command(""), state)
    state.set_state.assert_called_once()
    text = msg.answer.call_args.args[0]
    assert "Поиск" in text


async def test_search_with_arg_returns_results_directly(db_setup):
    """/search apple — сразу результаты, без FSM."""
    await _seed_catalog(db_setup)
    bot = ShopBot(_settings())
    msg = _fake_message()
    state = _fake_state()
    await bot._on_search_cmd(msg, _fake_command("apple"), state)
    # state.clear был вызван (без FSM)
    state.clear.assert_called_once()
    # Первый answer — результаты, второй — восстановление меню.
    assert msg.answer.call_count == 2
    first_text = msg.answer.call_args_list[0].args[0]
    assert "найдено 3" in first_text


async def test_search_no_matches_shows_empty_message(db_setup):
    await _seed_catalog(db_setup)
    bot = ShopBot(_settings())
    msg = _fake_message()
    state = _fake_state()
    await bot._on_search_cmd(msg, _fake_command("zzznotfound"), state)
    text = msg.answer.call_args.args[0]
    assert "ничего не нашлось" in text.lower()


async def test_search_fsm_query_handler(db_setup):
    """Пользователь в SearchState получает результаты по своему тексту."""
    await _seed_catalog(db_setup)
    bot = ShopBot(_settings())
    msg = _fake_message()
    msg.text = "apple"
    state = _fake_state()
    await bot._on_search_query(msg, state)
    state.clear.assert_called_once()
    # Должны быть результаты
    texts = [c.args[0] for c in msg.answer.call_args_list]
    assert any("найдено 3" in t for t in texts)


async def test_search_fsm_cancel_button(db_setup):
    """Кнопка «Отмена» в FSM-стейте корректно выходит из режима."""
    bot = ShopBot(_settings())
    msg = _fake_message()
    msg.text = "✖ Отмена"
    state = _fake_state()
    await bot._on_search_query(msg, state)
    state.clear.assert_called_once()


async def test_search_pagination_stores_results(db_setup):
    """При большом числе результатов появляется кнопка ›, ведущая на srh:."""
    async with db_setup() as s:
        for i in range(1, 20):  # 19 услуг с подстрокой 'mass'
            await upsert_catalog_service(
                s, ns_service_id=i, category_id=10, category_name="Cat",
                service_name=f"mass {i:02d}", ns_price_usd=1.0,
                rub_price_kopecks=10000 + i, in_stock=10, fields_json=None,
                base_name="Cat", group_slug=make_group_slug("Cat"),
            )
        await s.commit()

    bot = ShopBot(_settings())
    msg = _fake_message()
    state = _fake_state()
    await bot._on_search_cmd(msg, _fake_command("mass"), state)

    # Берём markup из первого answer (с результатами)
    first_call = msg.answer.call_args_list[0]
    markup = first_call.kwargs["reply_markup"]
    next_btn = next(
        (btn for row in markup.inline_keyboard for btn in row
         if btn.callback_data and btn.callback_data.startswith("srh:")
         and btn.callback_data.endswith(":1")),
        None,
    )
    assert next_btn is not None, "На странице 0 должна быть кнопка ›"

    # Кликаем «следующая страница»
    cb = _fake_cb(next_btn.callback_data)
    await bot._on_cb_search_page(cb)
    cb.message.edit_text.assert_called_once()
    text2 = cb.message.edit_text.call_args.kwargs["text"]
    assert "стр. 2" in text2 or "2 из" in text2


async def test_search_session_expired_alerts_user(db_setup):
    bot = ShopBot(_settings())
    cb = _fake_cb("srh:deadbeef:0")
    await bot._on_cb_search_page(cb)
    cb.answer.assert_called_once()
    assert cb.answer.call_args.kwargs.get("show_alert") is True
