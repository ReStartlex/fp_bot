"""
Тесты для src/shop/taxonomy_icons.py — pure-функции, легко покрываются.

Проверяем:
  - brand_emoji: правильный матч для известных брендов, default для незнакомых.
  - region_flag: правильный флаг для разных форматов category_name.
  - stock_bar / stock_status_text: корректные индикаторы при разных stock.
  - featured_badge: фактические эмодзи для топ-3 и пусто для остальных.
"""
from __future__ import annotations

from src.shop.taxonomy_icons import (
    DEFAULT_BRAND_EMOJI,
    FEATURED_BADGES,
    brand_emoji,
    featured_badge,
    region_flag,
    stock_bar,
    stock_status_text,
)


# ─── brand_emoji ───────────────────────────────────────────────────


def test_brand_emoji_apple():
    assert brand_emoji("Apple Gift Card") == "🍎"
    assert brand_emoji("apple gift card") == "🍎"
    assert brand_emoji("Apple iTunes") == "🍎"


def test_brand_emoji_steam():
    assert brand_emoji("Steam Wallet Code") == "🎮"
    assert brand_emoji("STEAM") == "🎮"


def test_brand_emoji_roblox():
    assert brand_emoji("Roblox") == "🎲"
    assert brand_emoji("Robux Card") == "🎲"


def test_brand_emoji_playstation():
    assert brand_emoji("PlayStation®Store Wallet") == "🎮"
    assert brand_emoji("PSN voucher") == "🎮"


def test_brand_emoji_blizzard():
    assert brand_emoji("Blizzard Gift Card") == "❄"
    assert brand_emoji("Battle.net Balance") == "❄"


def test_brand_emoji_pubg():
    assert brand_emoji("Pubg Mobile Gift Card") == "🪖"


def test_brand_emoji_unknown_returns_default():
    """Неизвестный бренд — нейтральный 🛒, не пустота."""
    assert brand_emoji("Что-то Никому Неизвестное") == DEFAULT_BRAND_EMOJI
    assert brand_emoji("Random Random Random") == DEFAULT_BRAND_EMOJI


def test_brand_emoji_empty_string():
    """Пустая строка — default, а не падение."""
    assert brand_emoji("") == DEFAULT_BRAND_EMOJI


def test_brand_emoji_partial_match_in_long_name():
    """В длинном названии должен сматчиться по подстроке."""
    assert brand_emoji("IDR 6000 Steam Wallet Code") == "🎮"


def test_brand_emoji_minecraft():
    assert brand_emoji("Minecraft Coins") == "⛏"


def test_brand_emoji_spotify():
    assert brand_emoji("Spotify Premium 3 months") == "🎵"


# ─── region_flag ───────────────────────────────────────────────────


def test_region_flag_us():
    assert region_flag("Apple Gift Card | US") == "🇺🇸"
    assert region_flag("Apple Gift Card | USA") == "🇺🇸"


def test_region_flag_turkey():
    assert region_flag("Apple Gift Card | TR | 10 TRY") == "🇹🇷"


def test_region_flag_eu():
    assert region_flag("Blizzard Gift Card | EU | 20 EUR") == "🇪🇺"


def test_region_flag_brazil():
    assert region_flag("Apple Gift Card | BR | 100 BRL") == "🇧🇷"


def test_region_flag_uae():
    assert region_flag("Steam Wallet Code | AE | 50 AED") == "🇦🇪"


def test_region_flag_hk():
    assert region_flag("Steam Wallet Code | HK | 100 HKD") == "🇭🇰"


def test_region_flag_indonesia_by_currency():
    """IDR — флаг Индонезии (по валюте, не по стране)."""
    assert region_flag("IDR 6000 Steam Wallet Code") == "🇮🇩"


def test_region_flag_global():
    assert region_flag("Roblox | Global | 100 Robux") == "🌍"


def test_region_flag_no_match_returns_empty():
    """Если флаг не найден — пустая строка, не «default»."""
    assert region_flag("Spotify Premium") == ""
    assert region_flag("") == ""


def test_region_flag_first_match_wins():
    """Если несколько кандидатов — первый по порядку токенов."""
    # «TR» идёт раньше «USD»
    assert region_flag("TR voucher USD") == "🇹🇷"


def test_region_flag_strips_punctuation():
    """`$5`, `(USD)` — должны нормализоваться к чистому коду."""
    assert region_flag("Gift Card $5 USD") == "🇺🇸"


# ─── stock_bar ─────────────────────────────────────────────────────


def test_stock_bar_full():
    assert stock_bar(10, cap=10) == "🟩🟩🟩🟩🟩"


def test_stock_bar_zero():
    assert stock_bar(0, cap=10) == "⬜⬜⬜⬜⬜"


def test_stock_bar_partial():
    # 7/10 ≈ 4/5 → 4 заполненных
    assert stock_bar(7, cap=10) == "🟩🟩🟩🟩⬜"


def test_stock_bar_low_but_nonzero_shows_at_least_one():
    """1 из 100 не должен показывать «пусто» — это вводит в заблуждение."""
    result = stock_bar(1, cap=100)
    assert "🟩" in result, "При in_stock>0 должен быть хотя бы один зелёный"


def test_stock_bar_above_cap_is_full():
    """50 из cap=10 — всё равно full bar (а не «overflow»)."""
    assert stock_bar(50, cap=10) == "🟩🟩🟩🟩🟩"


def test_stock_bar_negative_treated_as_empty():
    """Отрицательное (защита от грязных данных) — empty."""
    assert stock_bar(-5, cap=10) == "⬜⬜⬜⬜⬜"


# ─── stock_status_text ─────────────────────────────────────────────


def test_stock_status_text_out_of_stock():
    text = stock_status_text(0)
    assert "🚫" in text
    assert "Нет в наличии" in text


def test_stock_status_text_low():
    """1..4 → ⚠ + точная цифра."""
    text = stock_status_text(3)
    assert "⚠" in text
    assert "3" in text
    assert "мало" in text.lower()


def test_stock_status_text_medium():
    """5..9 → 🟢 + цифра."""
    text = stock_status_text(7)
    assert "🟢" in text
    assert "7" in text


def test_stock_status_text_high():
    """≥10 → 🟢 + цифра (но без «осталось», тон позитивный)."""
    text = stock_status_text(50)
    assert "🟢" in text
    assert "50" in text


# ─── featured_badge ────────────────────────────────────────────────


def test_featured_badge_top_three():
    assert featured_badge(0) == FEATURED_BADGES[0]
    assert featured_badge(1) == FEATURED_BADGES[1]
    assert featured_badge(2) == FEATURED_BADGES[2]


def test_featured_badge_beyond_top_is_empty():
    """rank=3+ — без бейджа."""
    assert featured_badge(3) == ""
    assert featured_badge(10) == ""


def test_featured_badge_negative_is_empty():
    """Защита от грязного rank."""
    assert featured_badge(-1) == ""


def test_featured_badges_count():
    """Текущая система: ровно три уровня (🔥 ⭐ 💎)."""
    assert len(FEATURED_BADGES) == 3
