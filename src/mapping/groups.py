"""Автоклассификация FunPay/NS лотов по товарным группам."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DefaultLotGroup:
    slug: str
    name: str
    keywords: tuple[str, ...]
    sort_order: int
    markup_percent: float | None = None
    stock_cap: int | None = None


DEFAULT_LOT_GROUPS: tuple[DefaultLotGroup, ...] = (
    DefaultLotGroup(
        slug="app-store-itunes",
        name="App Store & iTunes",
        keywords=("app store", "itunes", "apple gift", "apple", "itunes"),
        sort_order=10,
    ),
    DefaultLotGroup(
        slug="battle-net",
        name="Battle.net",
        keywords=("battle.net", "battle net", "battlenet", "blizzard"),
        sort_order=20,
    ),
    DefaultLotGroup(
        slug="steam",
        name="Steam",
        keywords=("steam",),
        sort_order=30,
    ),
    DefaultLotGroup(
        slug="pubg-mobile",
        name="PUBG Mobile",
        keywords=("pubg", "pubg mobile"),
        sort_order=40,
    ),
    DefaultLotGroup(
        slug="playstation",
        name="PlayStation",
        keywords=("playstation", "psn", "ps store"),
        sort_order=50,
    ),
    DefaultLotGroup(
        slug="xbox",
        name="Xbox",
        keywords=("xbox", "microsoft"),
        sort_order=60,
    ),
)


def normalize_group_text(value: str | None) -> str:
    raw = (value or "").lower().replace("ё", "е")
    raw = re.sub(r"[^a-zа-я0-9.+]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def group_keywords_to_text(keywords: tuple[str, ...] | list[str] | str | None) -> str:
    if keywords is None:
        return ""
    if isinstance(keywords, str):
        return keywords
    return "\n".join(k.strip() for k in keywords if k and k.strip())


def parse_group_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    keywords: list[str] = []
    for line in re.split(r"[\n,;]+", value):
        text = normalize_group_text(line)
        if text:
            keywords.append(text)
    return keywords


def group_match_score(text: str | None, keywords: str | None) -> int:
    haystack = normalize_group_text(text)
    if not haystack:
        return 0
    score = 0
    for keyword in parse_group_keywords(keywords):
        if not keyword:
            continue
        if keyword in haystack:
            score = max(score, 100 + len(keyword))
        elif all(part in haystack.split() for part in keyword.split()):
            score = max(score, 50 + len(keyword))
    return score
