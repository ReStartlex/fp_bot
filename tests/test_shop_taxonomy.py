"""
Тесты shop-таксономии: парсинг имени NS-категории на (base_name, variant)
и стабильный slug для группы. Используется чтобы свернуть однотипные
региональные/платформенные NS-категории в одну группу для UI.
"""
from __future__ import annotations

import pytest

from src.shop.taxonomy import make_group_slug, parse_category_name


# ─── parse_category_name ────────────────────────────────────────────


def test_no_separator_returns_full_name():
    assert parse_category_name("EA SPORTS FC™ Mobile") == (
        "EA SPORTS FC™ Mobile", None
    )


def test_single_pipe_splits_into_base_and_variant():
    assert parse_category_name("Apple Gift Card | US") == (
        "Apple Gift Card", "US"
    )


def test_multi_pipe_keeps_rest_as_variant():
    """'A | B | C' → base='A', variant='B | C' (для редких 3-уровневых имён)."""
    assert parse_category_name("EA SPORTS FC™ 25 | GLOB | Xbox Games") == (
        "EA SPORTS FC™ 25", "GLOB | Xbox Games"
    )


def test_trims_whitespace_around_pipe():
    assert parse_category_name("Apple   |   EU") == ("Apple", "EU")


def test_handles_tight_pipe_without_spaces():
    """NS иногда возвращает 'X|Y' без пробелов — тоже парсим."""
    assert parse_category_name("Steam|RU") == ("Steam", "RU")


def test_empty_string_returns_empty_base():
    assert parse_category_name("") == ("", None)


def test_only_pipe_returns_empty_strings():
    # Невероятно, но защитимся: на UI отдадим как "Без категории"
    assert parse_category_name(" | ") == ("", "")


# ─── make_group_slug ────────────────────────────────────────────────


def test_slug_is_stable():
    """Одно и то же имя → одинаковый slug между вызовами."""
    assert make_group_slug("Apple") == make_group_slug("Apple")


def test_slug_is_case_insensitive():
    """'apple' и 'APPLE' и 'Apple' дают один slug — нормализуем регистр."""
    assert make_group_slug("Apple") == make_group_slug("APPLE")
    assert make_group_slug("Apple") == make_group_slug("apple")


def test_slug_short_enough_for_callback_data():
    """Telegram callback_data ≤64 байт. Используем slug ≤16."""
    slug = make_group_slug("Some Very Long Category Name With Many Words And ™ Marks 2026")
    assert len(slug) <= 16
    assert slug.isalnum()


def test_slug_different_names_collide_rarely():
    """Sanity: разные имена дают разные slug'и (вероятность коллизии для
    SHA1[:10] ничтожна, но проверим явно)."""
    names = [
        "Apple", "Steam", "Spotify", "EA Gift Card", "Dishonored®",
        "EA SPORTS FC™ 24", "EA SPORTS FC™ 25", "EA SPORTS FC™ Mobile",
        "Dying Light", "Dragon's Dogma 2",
    ]
    slugs = {make_group_slug(n) for n in names}
    assert len(slugs) == len(names)


def test_slug_strips_whitespace():
    """' Apple ' и 'Apple' — одна и та же группа."""
    assert make_group_slug("Apple") == make_group_slug(" Apple ")
