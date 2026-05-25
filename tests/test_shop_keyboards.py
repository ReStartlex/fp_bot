"""
Тесты shop-клавиатур: pure-функции, генерящие Reply/InlineKeyboard
для UI магазина. Тестируются изолированно — без поднятия aiogram-бота,
БД или сети.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.shop.keyboards import (
    CATALOG_GROUPS_PAGE_SIZE,
    VARIANTS_GRID_COLS,
    balance_keyboard,
    cancel_keyboard,
    catalog_groups_keyboard,
    main_menu_keyboard,
    referrals_keyboard,
    search_results_keyboard,
    service_card_keyboard,
    services_page_keyboard,
    variants_grid_keyboard,
)


# ─── main_menu_keyboard ─────────────────────────────────────────────


def test_main_menu_has_six_actions():
    kb = main_menu_keyboard()
    labels = [b.text for row in kb.keyboard for b in row]
    # Все 6 разделов должны быть на клавиатуре
    assert any("Каталог" in t for t in labels)
    assert any("Поиск" in t for t in labels)
    assert any("Баланс" in t for t in labels)
    assert any("Заказы" in t for t in labels)
    assert any("Реферал" in t for t in labels)
    assert any("Поддержка" in t for t in labels)


def test_main_menu_is_resize_persistent():
    """resize_keyboard=True → компактные кнопки на любом устройстве."""
    kb = main_menu_keyboard()
    assert kb.resize_keyboard is True
    # is_persistent=True — клавиатура остаётся при сворачивании
    assert kb.is_persistent is True


def test_main_menu_layout_is_2x3():
    """3 ряда по 2 кнопки — самая удобная сетка для мобилки."""
    kb = main_menu_keyboard()
    assert len(kb.keyboard) == 3
    for row in kb.keyboard:
        assert len(row) == 2


# ─── cancel_keyboard ────────────────────────────────────────────────


def test_cancel_keyboard_has_cancel():
    kb = cancel_keyboard()
    flat = [b.text for row in kb.keyboard for b in row]
    assert any("Отмена" in t for t in flat)


# ─── catalog_groups_keyboard (пагинация) ────────────────────────────


def _mk_group(slug: str, name: str, variants: int = 1, services: int = 1,
              cheapest: int = 10000):
    return SimpleNamespace(
        group_slug=slug, base_name=name,
        variants_count=variants, services_count=services,
        cheapest_price_kopecks=cheapest,
    )


def test_catalog_no_groups_returns_empty_state():
    text, kb = catalog_groups_keyboard(groups=[], page=0)
    assert "пуст" in text.lower()
    # Клавиатура есть и содержит хотя бы кнопку «закрыть» или поиск
    assert kb is not None


def test_catalog_first_page_shows_only_page_size_groups():
    """20 групп → на странице 0 видно ровно PAGE_SIZE."""
    groups = [_mk_group(f"s{i:02}", f"Cat {i:02}") for i in range(20)]
    text, kb = catalog_groups_keyboard(groups=groups, page=0)
    group_buttons = [
        btn for row in kb.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("grp:")
    ]
    assert len(group_buttons) == CATALOG_GROUPS_PAGE_SIZE


def test_catalog_pagination_buttons_appear_when_needed():
    """Когда групп > PAGE_SIZE, виден nav ‹ / страница / ›."""
    groups = [_mk_group(f"s{i:02}", f"Cat {i:02}") for i in range(20)]
    _, kb = catalog_groups_keyboard(groups=groups, page=0)
    nav_btns = [
        btn for row in kb.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("cats:")
    ]
    # На стр.0 — должна быть кнопка «вперёд» (cats:1)
    assert any("cats:1" in (b.callback_data or "") for b in nav_btns)


def test_catalog_no_pagination_when_one_page():
    """Когда групп ≤ PAGE_SIZE — нет навигации."""
    groups = [_mk_group(f"s{i}", f"Cat {i}") for i in range(3)]
    _, kb = catalog_groups_keyboard(groups=groups, page=0)
    nav_btns = [
        btn for row in kb.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("cats:")
    ]
    assert nav_btns == []


def test_catalog_header_shows_page_counter():
    """Заголовок включает 'стр. X из Y · всего N'."""
    groups = [_mk_group(f"s{i:02}", f"Cat {i:02}") for i in range(25)]
    text, _ = catalog_groups_keyboard(groups=groups, page=0)
    assert "1" in text and "3" in text  # 25 / 10 = 3 страницы
    assert "25" in text  # всего


def test_catalog_page_normalized_when_too_high():
    """Если запросили несуществующую страницу — нормализуем на последнюю."""
    groups = [_mk_group(f"s{i:02}", f"Cat {i:02}") for i in range(15)]
    text, kb = catalog_groups_keyboard(groups=groups, page=999)
    # Покажет последнюю страницу (1 — индекс 1)
    group_buttons = [
        btn for row in kb.inline_keyboard for btn in row
        if btn.callback_data and btn.callback_data.startswith("grp:")
    ]
    # 15 групп, PAGE_SIZE=10 → на последней стр. 5 групп
    assert len(group_buttons) == 5


def test_catalog_label_includes_variants_when_multiple():
    g = _mk_group("s1", "Apple Gift Card", variants=13, services=20, cheapest=9077)
    _, kb = catalog_groups_keyboard(groups=[g], page=0)
    btn = next(
        b for row in kb.inline_keyboard for b in row
        if b.callback_data == "grp:s1:0"
    )
    assert "13" in btn.text
    assert "Apple" in btn.text


def test_catalog_label_singular_variant_no_count():
    g = _mk_group("s1", "Standalone", variants=1, services=3, cheapest=50000)
    _, kb = catalog_groups_keyboard(groups=[g], page=0)
    btn = next(
        b for row in kb.inline_keyboard for b in row
        if b.callback_data == "grp:s1:0"
    )
    # Для 1 варианта не показываем «1 вариант» — это шум
    assert "вариант" not in btn.text.lower()


# ─── variants_grid_keyboard (grid 2..3 столбца) ─────────────────────


def _mk_variant(cid: int, name: str, cnt: int = 1, cheapest: int = 10000):
    return SimpleNamespace(
        category_id=cid, category_name=name,
        services_count=cnt, cheapest_price_kopecks=cheapest,
    )


def test_variants_grid_uses_two_columns_for_short_names():
    """Короткие варианты (страны 'US'/'EU') — рендерим в 2 столбца."""
    variants = [
        _mk_variant(i, f"Apple Gift Card | {region}", 1)
        for i, region in enumerate(["US", "EU", "UK", "DE", "FR", "JP"], start=1)
    ]
    _, kb = variants_grid_keyboard(variants=variants, base_name="Apple Gift Card")
    # Кнопки вариантов
    var_buttons = [
        row for row in kb.inline_keyboard
        if any(b.callback_data and b.callback_data.startswith("cat:") for b in row)
    ]
    # Должны быть ряды по VARIANTS_GRID_COLS (=2) кнопок
    for row in var_buttons:
        assert len(row) <= VARIANTS_GRID_COLS


def test_variants_grid_strips_base_name_prefix():
    """В кнопке варианта показываем только хвост после `|`, без префикса."""
    variants = [
        _mk_variant(1, "Apple Gift Card | US", cheapest=40000),
    ]
    _, kb = variants_grid_keyboard(variants=variants, base_name="Apple Gift Card")
    btn = next(
        b for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("cat:")
    )
    # «Apple Gift Card» уже в заголовке — на кнопке только «US»
    assert "Apple Gift Card" not in btn.text
    assert "US" in btn.text


def test_variants_grid_has_back_to_catalog():
    variants = [_mk_variant(1, "X | US")]
    _, kb = variants_grid_keyboard(variants=variants, base_name="X")
    back_btn = next(
        (b for row in kb.inline_keyboard for b in row
         if b.callback_data and b.callback_data.startswith("cats:")),
        None,
    )
    assert back_btn is not None


# ─── services_page_keyboard ─────────────────────────────────────────


def _mk_svc(sid: int, name: str, price: int = 10000, stock: int = 50):
    return SimpleNamespace(
        ns_service_id=sid, service_name=name,
        rub_price_kopecks=price, in_stock=stock,
        category_name="Cat", category_id=10,
    )


def test_services_page_back_button_points_to_group_when_provided():
    """Если services открыты из drill-down группы — кнопка назад на группу,
    а не в общий список категорий."""
    svcs = [_mk_svc(1, "A")]
    _, kb = services_page_keyboard(
        services=svcs, total=1, category_id=10, page=0, group_slug="apple123",
    )
    back_btns = [
        b for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("grp:apple123")
    ]
    assert back_btns, "Должна быть кнопка возврата к группе"


def test_services_page_back_to_catalog_when_no_group():
    svcs = [_mk_svc(1, "A")]
    _, kb = services_page_keyboard(
        services=svcs, total=1, category_id=10, page=0, group_slug=None,
    )
    back_btns = [
        b for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("cats:")
    ]
    assert back_btns


# ─── service_card_keyboard ──────────────────────────────────────────


def test_service_card_buy_button_for_in_stock():
    svc = _mk_svc(1, "Apple $5", price=40000, stock=10)
    _, kb = service_card_keyboard(svc=svc, group_slug="s1")
    buy_btns = [
        b for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("buy:")
    ]
    assert len(buy_btns) == 1


def test_service_card_no_buy_for_oos():
    svc = _mk_svc(1, "Apple $5", price=40000, stock=0)
    _, kb = service_card_keyboard(svc=svc, group_slug="s1")
    buy_btns = [
        b for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("buy:")
    ]
    assert buy_btns == []


# ─── search_results_keyboard ────────────────────────────────────────


def test_search_results_pagination():
    items = [_mk_svc(i, f"x {i}") for i in range(1, 20)]
    _, kb = search_results_keyboard(
        page_items=items[:8], total=19, page=0, session_id="abcd1234",
        query="x",
    )
    nav = [
        b for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("srh:abcd1234:")
    ]
    assert any("srh:abcd1234:1" in (b.callback_data or "") for b in nav)


def test_search_results_no_pagination_when_one_page():
    items = [_mk_svc(i, f"x {i}") for i in range(1, 5)]
    _, kb = search_results_keyboard(
        page_items=items, total=4, page=0, session_id="abcd1234",
        query="x",
    )
    nav = [
        b for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("srh:abcd1234:")
    ]
    assert nav == []


# ─── balance_keyboard ───────────────────────────────────────────────


def test_balance_keyboard_shows_all_stats():
    text, kb = balance_keyboard(
        current_kopecks=12345, earned_kopecks=20000,
        spent_kopecks=7655, operations_count=5, invited_count=2,
    )
    assert "123,45" in text  # current
    assert "200" in text     # earned
    assert "76,55" in text   # spent
    assert "5" in text       # operations


def test_balance_keyboard_has_topup_buttons():
    _, kb = balance_keyboard(
        current_kopecks=0, earned_kopecks=0, spent_kopecks=0,
        operations_count=0, invited_count=0,
    )
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "topup:crypto" in callbacks
    assert "topup:stars" in callbacks
    assert "topup:card" in callbacks
    assert "bal_hist:0" in callbacks
    assert "ref" in callbacks


def test_balance_keyboard_zero_state_shows_invite_hint():
    text, _ = balance_keyboard(
        current_kopecks=0, earned_kopecks=0, spent_kopecks=0,
        operations_count=0, invited_count=0,
    )
    assert "пригласи" in text.lower() or "рефералов" in text.lower()


def test_balance_keyboard_invited_state_shows_thanks():
    text, _ = balance_keyboard(
        current_kopecks=5000, earned_kopecks=5000, spent_kopecks=0,
        operations_count=1, invited_count=3,
    )
    assert "3" in text
    assert "neurodrop" in text.lower() or "NeuroDrop" in text


# ─── referrals_keyboard ─────────────────────────────────────────────


def test_referrals_keyboard_has_share_and_copy():
    text, kb = referrals_keyboard(
        ref_link="https://t.me/neirodropi_bot?start=ref_1",
        invited_count=0, earned_kopecks=0, active_referrals_count=0,
        bonus_percent=1.0,
    )
    share_btn = next(
        (b for row in kb.inline_keyboard for b in row if b.url),
        None,
    )
    assert share_btn is not None
    assert "t.me/share" in (share_btn.url or "")

    copy_btn = next(
        (b for row in kb.inline_keyboard for b in row
         if b.switch_inline_query is not None),
        None,
    )
    assert copy_btn is not None
    assert "neirodropi_bot" in (copy_btn.switch_inline_query or "")


def test_referrals_keyboard_shows_stats():
    text, _ = referrals_keyboard(
        ref_link="https://t.me/neirodropi_bot?start=ref_1",
        invited_count=311, earned_kopecks=1943500,
        active_referrals_count=287, bonus_percent=1.0,
    )
    assert "311" in text
    assert "287" in text
    # 1943500 кп = 19 435 ₽
    assert "19" in text and "435" in text
    assert "1%" in text  # бонус


def test_referrals_keyboard_link_in_text():
    text, _ = referrals_keyboard(
        ref_link="https://t.me/neirodropi_bot?start=ref_77",
        invited_count=0, earned_kopecks=0, active_referrals_count=0,
        bonus_percent=1.0,
    )
    assert "https://t.me/neirodropi_bot?start=ref_77" in text
