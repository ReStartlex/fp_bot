"""
Тесты генерации payload'ов и валидации против схемы FunPay-раздела.
"""
from __future__ import annotations

import textwrap

import pytest

from src.migrate.generator import (
    build_creation_fields,
    compute_price_rub,
    substitute,
    validate_entry_against_schema,
)
from src.migrate.loader import (
    MigrationEntry,
    MigrationService,
    load_entries,
    parse_entry,
)
from src.migrate.skeleton_yaml import TODO


def _entry(**over) -> MigrationEntry:
    base = dict(
        ns_category_id=4, ns_category_name="Apple | USA", platform="Apple",
        currency="USD", region_code="USA", region_ru="США", region_en="USA",
        funpay_node=1316, markup_percent=10.0,
        funpay_fields={"fields[currency]": "USD", "fields[usd]": "{nominal} USD"},
        summary_ru="Карта {platform} {nominal} {currency} ({region_ru})",
        summary_en="{platform} card {nominal} {currency} ({region_en})",
        desc_ru="код {nominal} {currency} для {platform}",
        desc_en="{nominal} {currency} {platform}",
        services=[
            MigrationService(20, 2, 1.9261, 1793),
            MigrationService(28, 10, 9.63, 3005),
        ],
    )
    base.update(over)
    return MigrationEntry(**base)


def _apple_schema() -> dict:
    """Урезанная схема node 1316: fields[currency] + fields[usd] селекты."""
    return {
        "url": "x",
        "inputs": [{"name": "price", "type": "text", "value": ""}],
        "selects": [
            {"name": "fields[currency]", "options": [
                {"value": "USD", "text": "USD", "selected": True},
                {"value": "BRL", "text": "BRL", "selected": False},
            ]},
            {"name": "fields[usd]", "options": [
                {"value": "2 USD", "text": "2 USD", "selected": False},
                {"value": "10 USD", "text": "10 USD", "selected": False},
                # номинала "9 USD" нет — услуга с ним должна отсеяться
            ]},
        ],
        "textareas": [
            {"name": "fields[summary][ru]", "value_preview": ""},
            {"name": "fields[desc][ru]", "value_preview": ""},
        ],
    }


# ───────────── substitute / price ─────────────

def test_substitute_all_tags():
    e = _entry()
    s = e.services[0]
    assert substitute("{platform} {nominal} {currency} ({region_ru}/{region_en})",
                      entry=e, service=s) == "Apple 2 USD (США/USA)"


def test_compute_price_rub():
    # 1.9261 USD × 1.10 × 90 = 190.68...
    assert compute_price_rub(1.9261, 10.0, 90.0) == pytest.approx(190.68, abs=0.01)


# ───────────── schema validation ─────────────

def test_validate_all_services_ok():
    e = _entry()
    v = validate_entry_against_schema(e, _apple_schema(), fx_rate=90.0)
    assert len(v.ok_services) == 2
    assert v.bad_services == []
    assert v.field_name_warnings == []


def test_validate_flags_unsupported_nominal():
    """Номинал, которого нет в селекте раздела → услуга отсеивается."""
    e = _entry(services=[
        MigrationService(20, 2, 1.93, 100),     # есть в схеме
        MigrationService(27, 9, 8.66, 50),      # "9 USD" нет в схеме
    ])
    v = validate_entry_against_schema(e, _apple_schema(), fx_rate=90.0)
    ok_ids = [s.service.service_id for s in v.ok_services]
    bad_ids = [s.service.service_id for s in v.bad_services]
    assert ok_ids == [20]
    assert bad_ids == [27]
    assert "9 USD" in v.bad_services[0].reasons[0]


def test_validate_flags_unknown_currency():
    """Валюта, которой нет в fields[currency] → все услуги отсеиваются."""
    e = _entry(currency="GBP", funpay_fields={"fields[currency]": "GBP"})
    v = validate_entry_against_schema(e, _apple_schema(), fx_rate=90.0)
    assert v.ok_services == []
    assert len(v.bad_services) == 2


def test_validate_warns_on_unknown_field_name():
    e = _entry(funpay_fields={"fields[currency]": "USD", "fields[bogus]": "x"})
    v = validate_entry_against_schema(e, _apple_schema(), fx_rate=90.0)
    assert any("fields[bogus]" in w for w in v.field_name_warnings)


def test_build_creation_fields_includes_summary_and_selects():
    e = _entry()
    fields = build_creation_fields(e, e.services[0], fx_rate=90.0)
    assert fields["fields[currency]"] == "USD"
    assert fields["fields[usd]"] == "2 USD"
    assert fields["fields[summary][ru]"] == "Карта Apple 2 USD (США)"
    assert fields["fields[desc][en]"] == "2 USD Apple"


# ───────────── loader / TODO detection ─────────────

def test_todo_problems_detects_unfilled():
    e = _entry(funpay_node=None, funpay_fields={TODO: TODO})
    problems = e.todo_problems()
    assert any("funpay_node" in p for p in problems)
    assert any(TODO in p for p in problems)


def test_ready_entry_has_no_problems():
    assert _entry().todo_problems() == []


def test_load_entries_roundtrip(tmp_path):
    yaml_text = textwrap.dedent("""
    - ns_category_id: 4
      ns_category_name: 'Apple | USA'
      platform: 'Apple'
      currency: USD
      region_code: USA
      region_ru: 'США'
      region_en: 'USA'
      funpay_node: 1316
      markup_percent: 10
      funpay_fields:
        "fields[currency]": 'USD'
        "fields[usd]": '{nominal} USD'
      summary_ru: 'Карта {nominal}'
      summary_en: 'Card {nominal}'
      desc_ru: 'd {nominal}'
      desc_en: 'd {nominal}'
      services:
        - { service_id: 20, nominal: 2, price_usd: 1.93, in_stock: 100 }
    """)
    p = tmp_path / "m.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    entries = load_entries(str(p))
    assert len(entries) == 1
    e = entries[0]
    assert e.funpay_node == 1316
    assert e.funpay_fields["fields[usd]"] == "{nominal} USD"
    assert e.services[0].nominal == 2
    assert e.todo_problems() == []
