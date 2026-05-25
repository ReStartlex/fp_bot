"""
Sprint 4 — Pro UX каталога: тесты на новые фичи keyboards.py.

Покрываем:
  * brand-emoji в catalog_groups_keyboard
  * featured-бейджи (🔥 ⭐ 💎) для топ-3 групп
  * country flags в variants_grid_keyboard
  * brand+flag в services_page_keyboard header
  * Pro service_card_keyboard: бэйджи, stock_bar, similar
  * Удалён устаревший «Оплата откроется в ближайшие дни»
  * search_results_keyboard: brand+flag в результатах поиска
  * list_similar_services: репозиторий
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base
from src.shop.keyboards import (
    catalog_groups_keyboard,
    search_results_keyboard,
    service_card_keyboard,
    services_page_keyboard,
    variants_grid_keyboard,
)
from src.shop.repo import list_similar_services, upsert_catalog_service
from src.shop.taxonomy import make_group_slug


# ─── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture()
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _fake_group(
    *, slug: str, base_name: str, variants_count: int = 1, price_kopecks: int = 10000,
) -> SimpleNamespace:
    """Минимальный stub для CategoryGroup, который ожидает catalog_groups_keyboard."""
    return SimpleNamespace(
        group_slug=slug,
        base_name=base_name,
        variants_count=variants_count,
        cheapest_price_kopecks=price_kopecks,
    )


def _fake_variant(*, category_id: int, category_name: str, price_kopecks: int) -> SimpleNamespace:
    return SimpleNamespace(
        category_id=category_id,
        category_name=category_name,
        cheapest_price_kopecks=price_kopecks,
    )


def _fake_service(
    *,
    ns_service_id: int,
    category_id: int,
    category_name: str,
    service_name: str,
    base_name: str | None,
    price_kopecks: int,
    in_stock: int,
    group_slug: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        ns_service_id=ns_service_id,
        category_id=category_id,
        category_name=category_name,
        service_name=service_name,
        base_name=base_name,
        rub_price_kopecks=price_kopecks,
        in_stock=in_stock,
        group_slug=group_slug,
    )


# ─── catalog_groups_keyboard: brand emoji + featured ──────────────


def test_catalog_groups_inject_brand_emoji_in_labels():
    """Каждая группа должна получить эмодзи бренда в кнопке."""
    groups = [
        _fake_group(slug="a", base_name="Apple Gift Card"),
        _fake_group(slug="s", base_name="Steam Wallet Code"),
        _fake_group(slug="r", base_name="Roblox"),
    ]
    _, markup = catalog_groups_keyboard(groups=groups, page=0)
    texts = [b.text for row in markup.inline_keyboard for b in row]
    apple_label = next(t for t in texts if "Apple" in t)
    steam_label = next(t for t in texts if "Steam" in t)
    roblox_label = next(t for t in texts if "Roblox" in t)
    assert "🍎" in apple_label
    assert "🎮" in steam_label
    assert "🎲" in roblox_label


def test_catalog_groups_top_three_get_featured_badges():
    """Топ-3 по variants_count получают 🔥 ⭐ 💎."""
    groups = [
        _fake_group(slug="a", base_name="Apple", variants_count=13),
        _fake_group(slug="s", base_name="Steam", variants_count=9),
        _fake_group(slug="r", base_name="Roblox", variants_count=5),
        _fake_group(slug="x", base_name="Spotify", variants_count=1),  # 4-й — без бейджа
    ]
    _, markup = catalog_groups_keyboard(groups=groups, page=0)
    texts = [b.text for row in markup.inline_keyboard for b in row]
    apple = next(t for t in texts if "Apple" in t)
    steam = next(t for t in texts if "Steam" in t)
    roblox = next(t for t in texts if "Roblox" in t)
    spotify = next(t for t in texts if "Spotify" in t)
    assert "🔥" in apple, f"Топ-1 должен получить 🔥, получили: {apple!r}"
    assert "⭐" in steam, f"Топ-2 должен получить ⭐, получили: {steam!r}"
    assert "💎" in roblox, f"Топ-3 должен получить 💎, получили: {roblox!r}"
    # 4-й — без featured-бейджа (но brand-эмодзи 🎵 должен быть)
    assert "🔥" not in spotify and "⭐" not in spotify and "💎" not in spotify
    assert "🎵" in spotify


def test_catalog_groups_pluralization_russian():
    """1 раздел, 2 раздела, 5 разделов — правильно склонения."""
    text_one, _ = catalog_groups_keyboard(
        groups=[_fake_group(slug="a", base_name="Apple")], page=0,
    )
    text_two, _ = catalog_groups_keyboard(
        groups=[
            _fake_group(slug="a", base_name="Apple"),
            _fake_group(slug="s", base_name="Steam"),
        ], page=0,
    )
    text_five, _ = catalog_groups_keyboard(
        groups=[_fake_group(slug=f"g{i}", base_name=f"B{i}") for i in range(5)],
        page=0,
    )
    assert "1 раздел " in text_one  # без окончания
    assert "2 раздела" in text_two
    assert "5 разделов" in text_five


def test_catalog_groups_empty_state_friendly_text():
    """Пустой каталог — не «empty», а тёплый текст с инструкцией."""
    text, markup = catalog_groups_keyboard(groups=[], page=0)
    assert "пуст" in text.lower()
    # Должна быть кнопка «Обновить»
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Обнов" in t for t in button_texts)


# ─── variants_grid_keyboard: country flags ─────────────────────────


def test_variants_grid_includes_country_flags():
    """Каждый региональный вариант должен иметь свой флаг."""
    variants = [
        _fake_variant(category_id=1, category_name="Apple Gift Card | US", price_kopecks=10000),
        _fake_variant(category_id=2, category_name="Apple Gift Card | EU", price_kopecks=12000),
        _fake_variant(category_id=3, category_name="Apple Gift Card | TR", price_kopecks=8000),
    ]
    _, markup = variants_grid_keyboard(variants=variants, base_name="Apple Gift Card")
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any("🇺🇸" in t for t in texts), "Должен быть флаг США"
    assert any("🇪🇺" in t for t in texts), "Должен быть флаг ЕС"
    assert any("🇹🇷" in t for t in texts), "Должен быть флаг Турции"


def test_variants_grid_header_has_brand_emoji():
    """Заголовок drill-down должен содержать brand-эмодзи."""
    variants = [
        _fake_variant(category_id=1, category_name="Apple Gift Card | US", price_kopecks=10000),
    ]
    text, _ = variants_grid_keyboard(variants=variants, base_name="Apple Gift Card")
    assert "🍎" in text


def test_variants_grid_empty_state_with_brand_emoji():
    """Пустой grid — эмодзи бренда сохраняется в заголовке (юзер видит «куда» он пришёл)."""
    text, _ = variants_grid_keyboard(variants=[], base_name="Roblox")
    assert "🎲" in text
    assert "Roblox" in text


# ─── services_page_keyboard: header brand+flag ─────────────────────


def test_services_page_header_has_brand_and_flag():
    """Заголовок страницы услуг — brand-эмодзи + флаг + название категории."""
    services = [
        _fake_service(
            ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US",
            service_name="Apple US $5", base_name="Apple Gift Card",
            price_kopecks=40000, in_stock=10,
        ),
    ]
    text, _ = services_page_keyboard(
        services=services, total=1,
        category_id=10, page=0, group_slug="abc",
    )
    assert "🍎" in text
    assert "🇺🇸" in text


def test_services_page_low_stock_marker_for_under_five():
    """В наличии 1..4 — ⚠ маркер. ≥5 — без маркера."""
    svc_low = _fake_service(
        ns_service_id=1, category_id=10,
        category_name="Apple US",
        service_name="Apple $5", base_name="Apple",
        price_kopecks=40000, in_stock=2,
    )
    svc_ok = _fake_service(
        ns_service_id=2, category_id=10,
        category_name="Apple US",
        service_name="Apple $10", base_name="Apple",
        price_kopecks=80000, in_stock=20,
    )
    _, markup = services_page_keyboard(
        services=[svc_low, svc_ok], total=2,
        category_id=10, page=0, group_slug=None,
    )
    low_btn = next(
        b for row in markup.inline_keyboard for b in row
        if "Apple $5" in b.text
    )
    ok_btn = next(
        b for row in markup.inline_keyboard for b in row
        if "Apple $10" in b.text
    )
    assert "⚠" in low_btn.text
    assert "2" in low_btn.text  # точная цифра
    assert "⚠" not in ok_btn.text


# ─── service_card_keyboard: Pro layout ─────────────────────────────


def test_service_card_has_brand_flag_in_title():
    svc = _fake_service(
        ns_service_id=1, category_id=10,
        category_name="Apple Gift Card | TR | 10 TRY",
        service_name="Apple TR 10 TRY", base_name="Apple Gift Card",
        price_kopecks=20500, in_stock=100,
    )
    text, _ = service_card_keyboard(svc=svc, group_slug=None)
    assert "🍎" in text
    assert "🇹🇷" in text


def test_service_card_in_stock_shows_trust_badges():
    """В наличии — бэйджи доверия: мгновенная выдача, гарантия."""
    svc = _fake_service(
        ns_service_id=1, category_id=10,
        category_name="Apple Gift Card | US",
        service_name="Apple US $5", base_name="Apple Gift Card",
        price_kopecks=40000, in_stock=50,
    )
    text, _ = service_card_keyboard(svc=svc, group_slug=None)
    assert "Мгновенная выдача" in text
    assert "Гарантия" in text


def test_service_card_oos_shows_different_badges():
    """OOS — НЕ показываем «мгновенная выдача» (это ложь)."""
    svc = _fake_service(
        ns_service_id=1, category_id=10,
        category_name="Apple Gift Card | US",
        service_name="Apple US $5", base_name="Apple Gift Card",
        price_kopecks=40000, in_stock=0,
    )
    text, _ = service_card_keyboard(svc=svc, group_slug=None)
    assert "Мгновенная выдача" not in text
    assert "Гарантия" in text  # гарантия всегда есть
    assert "Нет в наличии" in text


def test_service_card_buy_button_shows_price():
    """Кнопка «Купить» содержит цену — снижает frictiоn."""
    svc = _fake_service(
        ns_service_id=42, category_id=10,
        category_name="Apple US",
        service_name="Apple $5", base_name="Apple",
        price_kopecks=40000, in_stock=10,
    )
    _, markup = service_card_keyboard(svc=svc, group_slug=None)
    buy_btn = next(
        b for row in markup.inline_keyboard for b in row
        if "Купить" in b.text
    )
    assert "400" in buy_btn.text, f"Кнопка должна содержать цену 400₽: {buy_btn.text!r}"
    assert buy_btn.callback_data == "buy:42"


def test_service_card_no_buy_button_when_oos():
    """OOS — кнопки «Купить» нет."""
    svc = _fake_service(
        ns_service_id=42, category_id=10,
        category_name="Apple US",
        service_name="Apple $5", base_name="Apple",
        price_kopecks=40000, in_stock=0,
    )
    _, markup = service_card_keyboard(svc=svc, group_slug=None)
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert not any("Купить" in t for t in button_texts)


def test_service_card_stock_bar_in_text():
    """В наличии — текст содержит графический stock-bar (🟩…)."""
    svc = _fake_service(
        ns_service_id=1, category_id=10,
        category_name="Apple US",
        service_name="Apple $5", base_name="Apple",
        price_kopecks=40000, in_stock=10,
    )
    text, _ = service_card_keyboard(svc=svc, group_slug=None)
    assert "🟩" in text, "Должен быть графический stock-bar"


def test_service_card_no_obsolete_payment_unavailable_text():
    """После Sprint 3 фраза 'Оплата откроется в ближайшие дни' удалена."""
    svc = _fake_service(
        ns_service_id=1, category_id=10,
        category_name="Apple US",
        service_name="Apple $5", base_name="Apple",
        price_kopecks=40000, in_stock=10,
    )
    text, _ = service_card_keyboard(svc=svc, group_slug=None)
    assert "Оплата откроется в ближайшие дни" not in text


def test_service_card_similar_block_shows_alternatives():
    """Если переданы similar — для каждого создаётся кнопка «🔄 …»."""
    svc = _fake_service(
        ns_service_id=1, category_id=10,
        category_name="Apple US",
        service_name="Apple $5", base_name="Apple",
        price_kopecks=40000, in_stock=10,
    )
    similar = [
        _fake_service(
            ns_service_id=2, category_id=11,
            category_name="Apple TR",
            service_name="Apple TR 10 TRY", base_name="Apple",
            price_kopecks=20500, in_stock=50,
        ),
        _fake_service(
            ns_service_id=3, category_id=12,
            category_name="Apple EU",
            service_name="Apple EU €5", base_name="Apple",
            price_kopecks=45000, in_stock=20,
        ),
    ]
    _, markup = service_card_keyboard(svc=svc, group_slug=None, similar=similar)
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any("🔄" in t and "Apple TR" in t for t in button_texts)
    assert any("🔄" in t and "Apple EU" in t for t in button_texts)


def test_service_card_similar_excludes_self():
    """В similar не должно быть кнопки ведущей на сам же sid (защита)."""
    svc = _fake_service(
        ns_service_id=1, category_id=10,
        category_name="Apple US",
        service_name="Apple $5", base_name="Apple",
        price_kopecks=40000, in_stock=10,
    )
    similar_with_self = [
        _fake_service(
            ns_service_id=1, category_id=10,  # тот же sid
            category_name="Apple US",
            service_name="Apple $5", base_name="Apple",
            price_kopecks=40000, in_stock=10,
        ),
        _fake_service(
            ns_service_id=2, category_id=11,
            category_name="Apple TR",
            service_name="Apple TR 10 TRY", base_name="Apple",
            price_kopecks=20500, in_stock=50,
        ),
    ]
    _, markup = service_card_keyboard(
        svc=svc, group_slug=None, similar=similar_with_self,
    )
    callback_targets = [
        b.callback_data for row in markup.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("svc:")
    ]
    # Не должно быть svc:1 (ведёт на сам же), но должен быть svc:2.
    assert "svc:1" not in callback_targets
    assert "svc:2" in callback_targets


# ─── search_results_keyboard: brand+flag в результатах ─────────────


def test_search_results_have_brand_and_flag():
    services = [
        _fake_service(
            ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US",
            service_name="Apple US $5", base_name="Apple Gift Card",
            price_kopecks=40000, in_stock=10,
        ),
    ]
    _, markup = search_results_keyboard(
        page_items=services, total=1, page=0,
        session_id="abc12345", query="apple",
    )
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    apple_btn = next(t for t in button_texts if "Apple" in t)
    assert "🍎" in apple_btn
    assert "🇺🇸" in apple_btn


# ─── list_similar_services: repo ───────────────────────────────────


async def test_list_similar_returns_other_brand_services(db_session_factory):
    """list_similar_services вернёт другие услуги того же бренда."""
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_session_factory() as s:
        for sid, region, price in [
            (1, "US", 40000), (2, "TR", 20000), (3, "EU", 45000),
        ]:
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10 + sid,
                category_name=f"Apple Gift Card | {region}",
                service_name=f"Apple {region}",
                base_name="Apple Gift Card", group_slug=apple_slug,
                ns_price_usd=5.0, rub_price_kopecks=price,
                in_stock=10, fields_json=None,
            )
        await s.commit()

    async with db_session_factory() as s:
        similar = await list_similar_services(s, ns_service_id=1, limit=5)

    sids = [x.ns_service_id for x in similar]
    assert 1 not in sids, "Исходный сервис должен быть исключён"
    assert 2 in sids
    assert 3 in sids
    # Отсортировано по цене ASC (TR=20000 < EU=45000)
    assert sids == [2, 3]


async def test_list_similar_excludes_oos(db_session_factory):
    """OOS услуги бренда не должны попадать в «похожее»."""
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_session_factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple | US", service_name="Apple US",
            base_name="Apple Gift Card", group_slug=apple_slug,
            ns_price_usd=5.0, rub_price_kopecks=40000,
            in_stock=10, fields_json=None,
        )
        await upsert_catalog_service(
            s, ns_service_id=2, category_id=11,
            category_name="Apple | TR", service_name="Apple TR",
            base_name="Apple Gift Card", group_slug=apple_slug,
            ns_price_usd=5.0, rub_price_kopecks=20000,
            in_stock=0,  # OOS
            fields_json=None,
        )
        await s.commit()

    async with db_session_factory() as s:
        similar = await list_similar_services(s, ns_service_id=1)

    sids = [x.ns_service_id for x in similar]
    assert 2 not in sids, "OOS услуги не должны быть в similar"


async def test_list_similar_no_base_name_returns_empty(db_session_factory):
    """Если у origin нет base_name — пустой результат, без падения."""
    async with db_session_factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="X", service_name="X",
            base_name=None,  # нет
            group_slug=None,
            ns_price_usd=5.0, rub_price_kopecks=40000,
            in_stock=10, fields_json=None,
        )
        await s.commit()

    async with db_session_factory() as s:
        similar = await list_similar_services(s, ns_service_id=1)

    assert similar == []


async def test_list_similar_zero_limit_returns_empty(db_session_factory):
    """limit=0 → пустой результат (защита от бесполезных запросов)."""
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_session_factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple | US", service_name="Apple US",
            base_name="Apple Gift Card", group_slug=apple_slug,
            ns_price_usd=5.0, rub_price_kopecks=40000,
            in_stock=10, fields_json=None,
        )
        await s.commit()

    async with db_session_factory() as s:
        result = await list_similar_services(s, ns_service_id=1, limit=0)

    assert result == []
