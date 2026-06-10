"""
Тесты классификации и парсинга каталога NS для миграции.

Фикстуры — реальные строки из боевого каталога (дамп 2026-06-10):
разные форматы service_name, eligibility по order-fields.
"""
from __future__ import annotations

import yaml

from src.migrate.catalog import (
    build_skeleton_entry,
    classify_category,
    detect_currency,
    detect_platform,
    detect_region,
    extract_nominal,
)
from src.migrate.skeleton_yaml import TODO, render_skeleton
from src.ns.models import Category, FieldType, Service


def _qty_field() -> FieldType:
    return FieldType(key="quantity", type="int", name="Quantity", required=True)


def _acct_field() -> FieldType:
    return FieldType(key="account_number", type="str", name="Account", required=True)


def _svc(sid: int, name: str, price: float = 1.0, stock: int = 5) -> Service:
    return Service(
        service_id=sid, service_name=name, price=price,
        currency="USD", in_stock=stock,
    )


# ───────────────────────── eligibility ─────────────────────────

def test_quantity_only_category_is_eligible():
    cat = Category(
        category_id=4, category_name="Apple | USA",
        services=[_svc(20, "Apple Gift Card | USA | 2 USD")],
        fields=[_qty_field()],
    )
    e = classify_category(cat)
    assert e.eligible is True


def test_account_number_category_is_ineligible():
    """Top Up с account_number — нужен аккаунт покупателя, не мигрируем."""
    cat = Category(
        category_id=483, category_name="Free Fire Top Up",
        services=[_svc(1, "Free Fire 100 Diamonds")],
        fields=[_acct_field()],
    )
    e = classify_category(cat)
    assert e.eligible is False
    assert "account_number" in e.reason


def test_mixed_fields_ineligible():
    cat = Category(
        category_id=1, category_name="Steam Top-Up",
        services=[_svc(1, "Steam 10 USD")],
        fields=[_qty_field(), _acct_field()],
    )
    assert classify_category(cat).eligible is False


def test_eligible_but_no_stock_is_ineligible():
    cat = Category(
        category_id=6, category_name="Apple | RU",
        services=[_svc(1, "Apple Gift Card | RU | 1000 RUB", stock=0)],
        fields=[_qty_field()],
    )
    e = classify_category(cat)
    assert e.eligible is False
    assert "налич" in e.reason


# ───────────────────────── currency detection ─────────────────────────

def test_detect_currency_usd():
    services = [
        _svc(20, "Apple Gift Card | USA | 2 USD"),
        _svc(28, "Apple Gift Card | USA | 10 USD"),
    ]
    assert detect_currency(services) == "USD"


def test_detect_currency_prefix_format():
    services = [
        _svc(2267, "AED 50 Apple gift card"),
        _svc(2268, "AED 75 Apple gift card"),
    ]
    assert detect_currency(services) == "AED"


# ───────────────────────── region detection ─────────────────────────

def test_detect_region_known():
    assert detect_region("Apple | USA") == ("USA", "США", "USA")
    assert detect_region("Battle.net Gift Card | CA") == ("CA", "Канада", "Canada")


def test_detect_region_unknown_code_kept():
    code, ru, en = detect_region("Foo Gift Card | XX")
    assert code == "XX"
    assert ru is None and en is None


def test_detect_region_no_region():
    assert detect_region("Grand Theft Auto V") == (None, None, None)


def test_detect_platform_strips_giftcard_suffix():
    assert detect_platform("Apple | USA") == "Apple"
    assert detect_platform("Apple Gift Card | AE") == "Apple"
    assert detect_platform("Battle.net Gift Card | CA") == "Battle.net"
    assert detect_platform("Steam Wallet Code | USA") == "Steam"
    assert detect_platform("Playstation Gift Card | UK") == "Playstation"
    # значащие слова сохраняются
    assert detect_platform("Razer Gold Gift Card | TR") == "Razer Gold"
    assert detect_platform("Google Play Gift Code | US") == "Google Play"


# ───────────────────────── nominal extraction ─────────────────────────

def test_extract_nominal_pipe_format():
    assert extract_nominal("Apple Gift Card | USA | 2 USD", "USD") == 2
    assert extract_nominal("Apple Gift Card | TR | 1250 TRY", "TRY") == 1250


def test_extract_nominal_prefix_format():
    assert extract_nominal("AED 50 Apple gift card", "AED") == 50
    assert extract_nominal("EUR 15 Apple gift card", "EUR") == 15


def test_extract_nominal_double_space():
    # реальный кейс: "Apple Gift Card  | USA | 5 USD" (двойной пробел)
    assert extract_nominal("Apple Gift Card  | USA | 5 USD", "USD") == 5


def test_extract_nominal_large():
    assert extract_nominal("Apple Gift Card | IN | 10000 INR", "INR") == 10000


def test_extract_nominal_no_number():
    assert extract_nominal("Some weird name no digits", "USD") is None


# ───────────────────────── skeleton ─────────────────────────

def test_build_skeleton_entry_apple_usa():
    cat = Category(
        category_id=4, category_name="Apple | USA",
        services=[
            _svc(20, "Apple Gift Card | USA | 2 USD", price=1.9261, stock=1835),
            _svc(28, "Apple Gift Card | USA | 10 USD", price=9.63, stock=3005),
        ],
        fields=[_qty_field()],
    )
    entry = build_skeleton_entry(cat)
    assert entry.platform == "Apple"
    assert entry.currency == "USD"
    assert entry.region_ru == "США"
    assert [s.nominal for s in entry.services] == [2, 10]
    assert entry.parse_warnings == []


def test_skeleton_yaml_is_valid_and_has_todos():
    cat = Category(
        category_id=4, category_name="Apple | USA",
        services=[_svc(20, "Apple Gift Card | USA | 2 USD", price=1.93, stock=1835)],
        fields=[_qty_field()],
    )
    entry = build_skeleton_entry(cat)
    text = render_skeleton([entry])
    # YAML парсится
    data = yaml.safe_load(text)
    assert isinstance(data, list)
    assert data[0]["ns_category_id"] == 4
    assert data[0]["currency"] == "USD"
    # FunPay-сторона помечена TODO
    assert data[0]["funpay_node"] == TODO
    # шаблоны на месте и содержат теги
    assert "{nominal}" in data[0]["summary_ru"]
    assert "{platform}" in data[0]["summary_ru"]
    # услуги с распознанным номиналом
    assert data[0]["services"][0]["service_id"] == 20
    assert data[0]["services"][0]["nominal"] == 2


def test_skeleton_yaml_unknown_region_marks_todo():
    cat = Category(
        category_id=999, category_name="Foo | XX",
        services=[_svc(1, "Foo 5 USD", price=5.0, stock=3)],
        fields=[_qty_field()],
    )
    entry = build_skeleton_entry(cat)
    text = render_skeleton([entry])
    data = yaml.safe_load(text)
    assert data[0]["region_ru"] == TODO
