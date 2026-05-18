"""Тесты для модуля src.alerts.ui — клавиатуры и форматтеры."""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from aiogram.types import InlineKeyboardMarkup

from src.alerts import ui


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


# ─────────────── главное меню ───────────────


def test_main_menu_is_keyboard():
    kb = ui.main_menu()
    assert isinstance(kb, InlineKeyboardMarkup)
    assert len(kb.inline_keyboard) >= 4
    # каждая кнопка несёт callback_data
    for row in kb.inline_keyboard:
        for btn in row:
            assert btn.callback_data is not None
            assert btn.callback_data.startswith(("menu:", "close"))


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


def test_format_funpay_lot_line():
    lot = FakeLot(id=69300023, description="Apple Gift Card | USA 2 USD", price="500 ₽")
    out = ui.format_funpay_lot_line(lot)
    assert "69300023" in out
    assert "500" in out
    assert "Apple Gift Card" in out


def test_format_mapping_line_enabled():
    m = FakeMapping(
        funpay_lot_id=111,
        ns_service_id=222,
        enabled=True,
        markup_percent=15.0,
        stock_cap=50,
        label="Apple",
    )
    out = ui.format_mapping_line(m)
    assert "✅" in out
    assert "111" in out
    assert "222" in out
    assert "15.0%" in out
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
