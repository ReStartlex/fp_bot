"""Тесты для модуля src.alerts.ui — клавиатуры и форматтеры."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest
from aiogram.types import InlineKeyboardMarkup

from src.alerts import ui
from src.alerts.bot import TelegramBot, _format_order_line, format_percent


# ─────────────── фейковые объекты (как в FunPayAPI / NS) ───────────────


@dataclass
class FakeService:
    service_id: int
    service_name: str
    price: float
    currency: str
    in_stock: int


@dataclass
class FakeCategory:
    category_id: int
    category_name: str
    services: list


@dataclass
class FakeLot:
    id: int
    description: str
    price: str


@dataclass
class FakeMapping:
    funpay_lot_id: int
    ns_service_id: int
    enabled: bool
    markup_percent: float | None
    stock_cap: int | None
    label: str | None


@dataclass
class FakeOrder:
    created_at: datetime
    funpay_order_id: str
    status: str
    ns_custom_id: str | None


@dataclass
class FakeGroup:
    id: int
    name: str
    enabled: bool = True
    markup_percent: float | None = None
    stock_cap: int | None = None
    _mappings_count: int = 0
    _active_mappings_count: int = 0


# ─────────────── главное меню ───────────────


def test_main_menu_is_keyboard():
    kb = ui.main_menu()
    assert isinstance(kb, InlineKeyboardMarkup)
    assert len(kb.inline_keyboard) >= 4
    for row in kb.inline_keyboard:
        for btn in row:
            assert btn.callback_data is not None
            assert btn.callback_data.startswith(("menu:", "close", "target:"))


def test_main_menu_with_target_shows_clear_button():
    kb = ui.main_menu(target_lot_label="USA 2 USD (#69300023)")
    flat = [b for row in kb.inline_keyboard for b in row]
    target_btns = [b for b in flat if b.callback_data == "target:clear"]
    assert len(target_btns) == 1
    # И обязательно строка с целью присутствует
    assert any("USA 2 USD" in b.text for b in flat)


def test_main_menu_without_target_has_no_clear_button():
    kb = ui.main_menu(target_lot_label=None)
    flat = [b for row in kb.inline_keyboard for b in row]
    assert not any(b.callback_data == "target:clear" for b in flat)
    assert any(b.callback_data == f"menu:{ui.MENU_KIND_GROUPS}" for b in flat)


def test_pagination_row_when_single_page():
    assert ui.pagination_row("k", "abc", 0, 1) == []


def test_pagination_row_multiple_pages_wraps():
    row = ui.pagination_row("k", "abcd", 0, 5)
    assert len(row) == 3
    # ◀ должен вести на последнюю страницу (page=4)
    assert row[0].callback_data == "pg:k:abcd:4"
    # noop в центре
    assert row[1].callback_data == "noop"
    assert row[1].text == "1/5"
    # ▶ должен вести на page=1
    assert row[2].callback_data == "pg:k:abcd:1"


def test_pagination_row_last_page_wraps_to_first():
    row = ui.pagination_row("k", "abc", 4, 5)
    assert row[2].callback_data == "pg:k:abc:0"
    assert row[1].text == "5/5"


# ─────────────── форматтеры ───────────────


def test_format_ns_service_line_short_name():
    svc = FakeService(
        service_id=42, service_name="Apple USA 5", price=4.815, currency="USD", in_stock=999
    )
    out = ui.format_ns_service_line(svc)
    assert "42" in out
    assert "Apple USA 5" in out
    assert "USD" in out
    assert "999" in out


def test_format_ns_service_line_truncates_long_name():
    long = "x" * 200
    svc = FakeService(service_id=1, service_name=long, price=1.0, currency="USD", in_stock=0)
    out = ui.format_ns_service_line(svc)
    assert len(out) < 200


def test_format_ns_category_line():
    cat = FakeCategory(
        category_id=7,
        category_name="Apple Gift Card",
        services=[
            FakeService(1, "Apple 5", 5.0, "USD", 10),
            FakeService(2, "Apple 10", 10.0, "USD", 5),
        ],
    )
    out = ui.format_ns_category_line(cat)
    assert "Apple Gift Card" in out
    assert "2 услуг" in out
    assert "stock 15" in out


def test_format_funpay_lot_line_rounds_price():
    lot = FakeLot(id=69300023, description="Apple Gift Card | USA 2 USD", price=250.916497)
    out = ui.format_funpay_lot_line(lot)
    assert "69300023" in out
    assert "251" in out  # округление к целому при цене >= 100
    assert "250.916497" not in out
    assert "Apple Gift Card" in out


def test_format_funpay_lot_line_strips_emoji_from_title():
    lot = FakeLot(
        id=1, description="🔑 Подарочная карта Apple 🔵 2 USD (США) 🔵, USD, 2 USD",
        price="100",
    )
    out = ui.format_funpay_lot_line(lot)
    assert "🔑" not in out
    assert "🔵" not in out
    assert "Подарочная карта" in out


def test_format_mapping_line_enabled():
    m = FakeMapping(
        funpay_lot_id=111,
        ns_service_id=222,
        enabled=True,
        markup_percent=15.0,
        stock_cap=50,
        label="Apple Gift Card | USA | 2 USD",
    )
    out = ui.format_mapping_line(m)
    assert "✅" in out
    assert "111" in out
    assert "222" in out
    # для целого markup показываем без хвостового '.0'
    assert "15%" in out
    assert "15.0%" not in out
    assert "Apple" in out


def test_format_mapping_line_disabled():
    m = FakeMapping(
        funpay_lot_id=111,
        ns_service_id=222,
        enabled=False,
        markup_percent=None,
        stock_cap=None,
        label=None,
    )
    out = ui.format_mapping_line(m)
    assert "⏸" in out
    assert "default" in out


# ─────────────── label-хелперы для кнопок ───────────────


def test_short_title_strips_emoji_and_truncates():
    title = "🔑 Подарочная карта Apple 🔵 2 USD (США)"
    out = ui.short_title(title, limit=20)
    assert "🔑" not in out
    assert "🔵" not in out
    assert len(out) <= 20


def test_short_title_keeps_short_intact():
    out = ui.short_title("Apple 2 USD", limit=40)
    assert out == "Apple 2 USD"


def test_format_money_large_number_rounds_to_int():
    assert ui.format_money(250.916497) == "251"


def test_format_money_small_number_keeps_decimals():
    assert ui.format_money(1.9261) == "1.93"


def test_format_money_tiny_number():
    assert ui.format_money(0.0042) == "0.0042"


def test_format_money_with_suffix():
    out = ui.format_money(4.8152, suffix="USD")
    assert "USD" in out
    assert "4.82" in out


def test_format_money_non_numeric_returns_str():
    assert ui.format_money("—") == "—"


def test_ns_service_label_shows_name_and_price():
    svc = FakeService(
        service_id=20,
        service_name="Apple Gift Card | USA | 2 USD",
        price=1.9261,
        currency="USD",
        in_stock=1000,
    )
    out = ui.ns_service_label(svc)
    assert "USD" in out
    assert "1.93" in out or "2" in out
    assert len(out) <= 36


def test_funpay_lot_label_uses_title_not_id():
    lot = FakeLot(id=69300023, description="Apple Gift Card | USA 2 USD", price="250")
    out = ui.funpay_lot_label(lot, max_len=30)
    assert "69300023" not in out
    assert "Apple" in out


def test_lots_page_name_button_is_noop_not_target():
    """
    Нажатие на название лота не должно случайно переназначать target.
    Для назначения оставляем отдельную явную кнопку "🎯 Цель".
    """
    bot = object.__new__(TelegramBot)
    sess = type("Sess", (), {"items": [FakeLot(69300023, "Apple 2 USD", "147")]})()
    _, kb = bot._build_lots_page(  # type: ignore[attr-defined]
        sess,
        "abcd",
        sess.items,
        0,
        1,
    )
    rows = kb.inline_keyboard
    assert rows[0][0].callback_data == "noop"
    callbacks = [button.callback_data for row in rows for button in row]
    assert "act:fp_target:abcd:0" in callbacks


def test_groups_page_has_markup_controls():
    bot = object.__new__(TelegramBot)
    sess = type("Sess", (), {"items": [
        FakeGroup(
            id=1,
            name="Battle.net",
            markup_percent=12.5,
            _mappings_count=3,
            _active_mappings_count=2,
        )
    ]})()

    text, kb = bot._build_groups_page(  # type: ignore[attr-defined]
        sess, "abcd", sess.items, 0, 1
    )

    assert "Battle.net" in text
    callbacks = [button.callback_data for row in kb.inline_keyboard for button in row]
    assert "act:group_open:abcd:0" in callbacks
    assert "group:markup_set:1:12.5" in callbacks
    assert "group:markup_default:1" in callbacks


def test_clear_target_removes_menu_target_state():
    bot = object.__new__(TelegramBot)
    bot._target_lots = {123: 69300023}
    bot._target_labels = {123: "Apple 2 USD"}

    assert bot._target_label_for(123) == "Apple 2 USD (#69300023)"  # type: ignore[attr-defined]

    bot._clear_target(123)  # type: ignore[attr-defined]

    assert bot._target_label_for(123) is None  # type: ignore[attr-defined]
    assert 123 not in bot._target_lots
    assert 123 not in bot._target_labels


def test_mapping_label_falls_back_to_id():
    m = FakeMapping(
        funpay_lot_id=42,
        ns_service_id=1,
        enabled=True,
        markup_percent=None,
        stock_cap=None,
        label=None,
    )
    assert ui.mapping_label(m) == "#42"


def test_format_percent_integer_drops_decimal():
    assert format_percent(6) == "6"
    assert format_percent(6.0) == "6"
    assert format_percent(0) == "0"


def test_format_percent_fractional_trims_trailing_zeros():
    assert format_percent(5.5) == "5.5"
    assert format_percent(5.50) == "5.5"
    assert format_percent(7.25) == "7.25"
    # точность форматирования — до 2 знаков; хвостовые нули убираются
    assert format_percent(7.001) == "7"
    assert format_percent(7.1234) == "7.12"


def test_format_percent_none_and_invalid():
    assert format_percent(None) == "—"
    assert format_percent("hello") == "hello"


def test_format_mapping_line_uses_clean_percent_for_integer():
    @dataclass
    class FakeMappingLocal:
        funpay_lot_id: int
        ns_service_id: int
        enabled: bool
        markup_percent: float | None
        stock_cap: int | None
        label: str | None

    m = FakeMappingLocal(
        funpay_lot_id=1, ns_service_id=2, enabled=True,
        markup_percent=6.0, stock_cap=None, label="x",
    )
    line = ui.format_mapping_line(m)
    assert "6%" in line
    assert "6.0%" not in line


def test_format_mapping_line_keeps_fraction():
    @dataclass
    class FakeMappingLocal:
        funpay_lot_id: int
        ns_service_id: int
        enabled: bool
        markup_percent: float | None
        stock_cap: int | None
        label: str | None

    m = FakeMappingLocal(
        funpay_lot_id=1, ns_service_id=2, enabled=True,
        markup_percent=5.5, stock_cap=None, label="x",
    )
    assert "5.5%" in ui.format_mapping_line(m)


def test_format_order_line_uses_moscow_time():
    order = FakeOrder(
        created_at=datetime(2026, 5, 19, 15, 12),
        funpay_order_id="F2G4TM6U",
        status="delivered",
        ns_custom_id="ns-1",
    )
    out = _format_order_line(order)  # type: ignore[arg-type]
    assert "05-19 18:12 MSK" in out
    assert "#F2G4TM6U" in out


# ─────────────── render_list ───────────────


def test_render_list_empty():
    out = ui.render_list(
        page_items=[],
        formatter=str,
        title="X",
        page=0,
        total_pages=0,
        total_items=0,
    )
    assert "X" in out
    assert "Ничего не найдено" in out


def test_render_list_has_header_and_body():
    items = ["a", "b", "c"]
    out = ui.render_list(
        page_items=items,
        formatter=lambda x: f"- {x}",
        title="My list",
        page=1,
        total_pages=3,
        total_items=25,
    )
    assert "My list" in out
    assert "25" in out
    assert "2/3" in out  # page+1=2
    assert "- a" in out
    assert "- c" in out
    # одна строка заголовка (сейчас компактнее, чтобы повторы в чате
    # не растягивались на полэкрана)
    lines = out.splitlines()
    header_lines = [l for l in lines if "My list" in l]
    assert len(header_lines) == 1


def test_render_list_single_page_omits_page_marker():
    out = ui.render_list(
        page_items=["only"],
        formatter=lambda x: x,
        title="Solo",
        page=0,
        total_pages=1,
        total_items=1,
    )
    # При одной странице не должно быть «1/1» — лишний шум
    assert "1/1" not in out
    assert "Solo" in out
    assert "only" in out


# ─────────────── list_keyboard ───────────────


def test_list_keyboard_includes_menu_and_close_by_default():
    kb = ui.list_keyboard(kind="t", sid="abc", page=0, total_pages=1)
    assert isinstance(kb, InlineKeyboardMarkup)
    flat = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Меню" in t for t in flat)
    assert any("Закрыть" in t for t in flat)


def test_list_keyboard_includes_pagination_when_multi_page():
    kb = ui.list_keyboard(kind="t", sid="abc", page=0, total_pages=3)
    flat = [b for row in kb.inline_keyboard for b in row]
    has_prev = any(b.text == "◀" for b in flat)
    has_next = any(b.text == "▶" for b in flat)
    assert has_prev and has_next


def test_confirm_keyboard():
    kb = ui.confirm_keyboard(yes_data="do:42")
    assert isinstance(kb, InlineKeyboardMarkup)
    row = kb.inline_keyboard[0]
    assert row[0].callback_data == "do:42"
    assert row[1].callback_data == "close"
