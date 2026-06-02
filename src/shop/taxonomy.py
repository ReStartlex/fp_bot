"""
Таксономия shop-каталога: парсинг имён NS-категорий и группировка.

Зачем: NS отдаёт сотни «плоских» категорий вида
    Apple Gift Card | US
    Apple Gift Card | EU
    EA SPORTS FC™ 25 | GLOB | Xbox Games
    Dishonored® | ASIA
    Dishonored® | CIS
    ...
В UI каталога это превращается в нечитаемый длинный список. Сворачиваем
по «базовому имени» (до первого `|`): все региональные/платформенные
варианты одной игры/карты схлопываются в одну строку с drill-down.

Slug — стабильный 10-символьный hash, который умещается в Telegram
callback_data (макс. 64 байта).
"""
from __future__ import annotations

import hashlib


def parse_category_name(full_name: str) -> tuple[str, str | None]:
    """
    'Apple Gift Card | US' → ('Apple Gift Card', 'US').
    'EA SPORTS FC™ 25 | GLOB | Xbox Games' → ('EA SPORTS FC™ 25', 'GLOB | Xbox Games').
    'EA SPORTS FC™ Mobile' → ('EA SPORTS FC™ Mobile', None).

    Возвращает (base_name, variant). variant=None если разделителя нет.
    Пробелы вокруг `|` нормализуются.
    """
    if "|" not in full_name:
        return full_name.strip(), None
    head, _, tail = full_name.partition("|")
    return head.strip(), tail.strip()


def make_group_slug(base_name: str) -> str:
    """
    Стабильный slug для группы. Используется в callback_data вместо имени,
    которое может содержать пробелы, эмодзи, кириллицу и не помещается
    в 64-байтный лимит.

    Регистр и крайние пробелы игнорируются: «Apple», «apple», « Apple »
    → один slug.
    """
    normalized = base_name.strip().lower().encode("utf-8")
    return hashlib.sha1(normalized).hexdigest()[:10]
