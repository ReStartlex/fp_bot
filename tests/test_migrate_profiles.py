"""
Тесты профилей платформ и их применения в скелете.
"""
from __future__ import annotations

import textwrap

import yaml

from src.migrate.catalog import build_skeleton_entry
from src.migrate.profiles import load_profiles, profile_for
from src.migrate.skeleton_yaml import render_skeleton
from src.ns.models import Category, FieldType, Service


def _qty() -> FieldType:
    return FieldType(key="quantity", type="int", name="Quantity", required=True)


def _apple_cat() -> Category:
    return Category(
        category_id=4, category_name="Apple | USA",
        services=[Service(service_id=20, service_name="Apple Gift Card | USA | 2 USD",
                          price=1.93, currency="USD", in_stock=100)],
        fields=[_qty()],
    )


def test_load_profiles_normalizes_keys(tmp_path):
    p = tmp_path / "prof.yaml"
    p.write_text(textwrap.dedent("""
    Apple:
      desc_ru: 'мой Apple текст {nominal}'
      desc_en: 'my Apple text {nominal}'
    Steam:
      summary_ru: 'steam {nominal}'
    """), encoding="utf-8")
    profs = load_profiles(str(p))
    assert "apple" in profs
    assert profs["apple"]["desc_ru"] == "мой Apple текст {nominal}"
    assert profile_for(profs, "Apple")["desc_en"] == "my Apple text {nominal}"
    assert profile_for(profs, "nonexistent") == {}


def test_profile_applied_in_skeleton(tmp_path):
    profs = {"apple": {
        "desc_ru": "АКТИВАЦИЯ {nominal} {currency}: support.apple.com",
        "summary_ru": "Apple бренд {nominal}",
    }}
    entry = build_skeleton_entry(_apple_cat())
    text = render_skeleton([entry], profiles=profs)
    data = yaml.safe_load(text)
    # профиль применён
    assert "АКТИВАЦИЯ {nominal} {currency}" in data[0]["desc_ru"]
    assert data[0]["summary_ru"].rstrip("\n") == "Apple бренд {nominal}"
    # чего нет в профиле — дефолт (desc_en остался стандартным)
    assert "After Payment" in data[0]["desc_en"]


def test_no_profile_uses_defaults(tmp_path):
    entry = build_skeleton_entry(_apple_cat())
    text = render_skeleton([entry], profiles={})
    data = yaml.safe_load(text)
    assert "После оплаты" in data[0]["desc_ru"]
