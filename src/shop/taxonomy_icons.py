"""
Иконки и флаги для UI каталога shop-бота NeuroDrop.

Зачем
─────
Чтобы каталог визуально не выглядел монотонной стеной текстовых строк
вида «Apple Gift Card · 13 регионов · от 91 ₽», подмешиваем:

  * Эмодзи бренда — Apple, Steam, Roblox, и т.д. — рядом с базовым
    именем категории;
  * Флаг страны/региона — 🇺🇸 US, 🇹🇷 TR, 🇪🇺 EU — рядом с конкретным
    региональным вариантом.

Это не «декорация»: для покупателя флаг страны = мгновенно считываемый
маркер. Цепочка «прочитал → распарсил → понял» сокращается до одного
взгляда.

Принципы
────────
1. **Никакой магии**: всё через явные dict-маппинги. Если бренд не
   найден — возвращаем нейтральный 🛒. Если флаг не нашли — пустая
   строка (UI не ломается, просто без флага).
2. **Case-insensitive lookup**: «US» / «us» / «USA» — один и тот же
   флаг 🇺🇸. NS отдаёт коды как угодно, нормализуем здесь.
3. **Pure functions, no I/O**: модуль чисто детерминистский, легко
   тестируется без БД/сети.
4. **Расширяется без миграций**: новые бренды/коды просто добавляются
   в dict, не требуют изменений в БД.

Если NS поставщик добавит новый бренд/страну — его не будет, но UI
не ломается, просто без эмодзи. Постепенно расширяем список.
"""
from __future__ import annotations


# ─── Country / region flags ─────────────────────────────────────────
#
# Сюда идут коды стран (ISO 3166-1 alpha-2 и популярные альтернативы),
# а также коды валют, которые часто используются вместо стран в NS
# (например «USD», «BRL», «AED»).
#
# Ключи нормализуются в UPPER при lookup'е (см. region_flag).
COUNTRY_FLAGS: dict[str, str] = {
    # USA
    "US": "🇺🇸", "USA": "🇺🇸", "USD": "🇺🇸",
    # Europe
    "EU": "🇪🇺", "EUR": "🇪🇺",
    # UK
    "UK": "🇬🇧", "GB": "🇬🇧", "GBP": "🇬🇧",
    # Turkey
    "TR": "🇹🇷", "TRY": "🇹🇷",
    # Germany
    "DE": "🇩🇪",
    # Brazil
    "BR": "🇧🇷", "BRL": "🇧🇷",
    # UAE
    "AE": "🇦🇪", "AED": "🇦🇪", "UAE": "🇦🇪",
    # Hong Kong
    "HK": "🇭🇰", "HKD": "🇭🇰",
    # India
    "IN": "🇮🇳", "INR": "🇮🇳",
    # Indonesia
    "ID": "🇮🇩", "IDR": "🇮🇩",
    # Russia (на всякий — могут появиться карты для CIS)
    "RU": "🇷🇺", "RUB": "🇷🇺",
    # Saudi Arabia
    "SA": "🇸🇦", "SAR": "🇸🇦",
    # Japan
    "JP": "🇯🇵", "JPY": "🇯🇵",
    # Korea
    "KR": "🇰🇷", "KRW": "🇰🇷",
    # China
    "CN": "🇨🇳", "CNY": "🇨🇳",
    # Argentina
    "AR": "🇦🇷", "ARS": "🇦🇷",
    # Mexico
    "MX": "🇲🇽", "MXN": "🇲🇽",
    # Canada
    "CA": "🇨🇦", "CAD": "🇨🇦",
    # Australia
    "AU": "🇦🇺", "AUD": "🇦🇺",
    # Singapore
    "SG": "🇸🇬", "SGD": "🇸🇬",
    # Philippines
    "PH": "🇵🇭", "PHP": "🇵🇭",
    # Thailand
    "TH": "🇹🇭", "THB": "🇹🇭",
    # Malaysia
    "MY": "🇲🇾", "MYR": "🇲🇾",
    # Vietnam
    "VN": "🇻🇳", "VND": "🇻🇳",
    # Generic global
    "GLOBAL": "🌍", "GLOB": "🌍", "WORLD": "🌍",
    "CIS": "🌐", "ASIA": "🌏", "MENA": "🌍",
}


# ─── Brand emojis ───────────────────────────────────────────────────
#
# Подбираем по частичному совпадению лoercase base_name. Порядок
# важен: длинные ключи перед короткими (чтобы «playstation» не
# матчился по «play»).
#
# Парами (substring, emoji). Первый match выигрывает — обрабатываем
# в order.
BRAND_EMOJI_ORDER: list[tuple[str, str]] = [
    # Apple — все продукты эпла
    ("apple", "🍎"),
    ("itunes", "🍎"),
    ("ios", "🍎"),
    ("app store", "🍎"),
    # Sony / PlayStation
    ("playstation", "🎮"),
    ("psn", "🎮"),
    ("ps store", "🎮"),
    # Microsoft / Xbox
    ("xbox", "🕹"),
    ("microsoft", "💠"),
    ("windows", "🪟"),
    ("office", "📄"),
    # Steam / Valve
    ("steam wallet", "🎮"),
    ("steam", "🎮"),
    ("valve", "🎮"),
    # Nintendo
    ("nintendo", "🕹"),
    ("eshop", "🕹"),
    ("switch", "🕹"),
    # Blizzard / Battle.net
    ("blizzard", "❄"),
    ("battle.net", "❄"),
    ("battle net", "❄"),
    ("battlenet", "❄"),
    # Riot
    ("riot", "⚔"),
    ("league of legends", "⚔"),
    ("valorant", "⚔"),
    # Epic Games / Fortnite
    ("fortnite", "🎯"),
    ("epic games", "🎯"),
    ("epic store", "🎯"),
    # Roblox
    ("roblox", "🎲"),
    ("robux", "🎲"),
    # PUBG / mobile FPS
    ("pubg", "🪖"),
    ("call of duty", "🪖"),
    ("warzone", "🪖"),
    # MOBA / mobile
    ("mobile legends", "⚔"),
    ("clash royale", "👑"),
    ("clash of clans", "👑"),
    ("free fire", "🔥"),
    # Genshin / miHoYo
    ("genshin", "✨"),
    ("hoyoverse", "✨"),
    ("honkai", "✨"),
    # Streaming
    ("spotify", "🎵"),
    ("apple music", "🍎"),
    ("youtube", "▶"),
    ("netflix", "🎬"),
    ("disney", "🐭"),
    ("hbo", "🎬"),
    ("twitch", "📺"),
    # Cloud / VPN / Productivity
    ("google", "🔍"),
    ("amazon", "📦"),
    ("ebay", "🛒"),
    ("discord", "💬"),
    ("telegram", "✈"),
    # Crypto / cards (общие)
    ("visa", "💳"),
    ("mastercard", "💳"),
    ("paypal", "💸"),
    # Minecraft
    ("minecraft", "⛏"),
    # Mobile recharge / utility
    ("recharge", "📱"),
    ("voucher", "🎟"),
    ("gift card", "🎁"),
]


# Дефолтная иконка, если бренд не опознан. 🛒 — нейтрально-торговая,
# не вызывает «странного» ощущения у юзера. НЕ ставим эмодзи-фейс
# или эмодзи-животное — они тяжелее по weight, отвлекают.
DEFAULT_BRAND_EMOJI = "🛒"


def brand_emoji(base_name: str) -> str:
    """
    Возвращает эмодзи бренда по base_name категории.

    Lookup по partial-match (lowercase). Если ни один бренд не подошёл —
    возвращает DEFAULT_BRAND_EMOJI (🛒).

    Примеры:
      brand_emoji("Apple Gift Card") → "🍎"
      brand_emoji("Steam Wallet Code") → "🎮"
      brand_emoji("Roblox") → "🎲"
      brand_emoji("IDR 6000 Steam Wallet Code") → "🎮"  # частичный match
      brand_emoji("Что-то Незнакомое") → "🛒"
    """
    if not base_name:
        return DEFAULT_BRAND_EMOJI
    lower = base_name.lower()
    for substring, emoji in BRAND_EMOJI_ORDER:
        if substring in lower:
            return emoji
    return DEFAULT_BRAND_EMOJI


def region_flag(category_or_variant: str) -> str:
    """
    Возвращает флаг страны для category_name или variant.

    Логика:
      1. Если в строке есть «|» — берём хвост после «|» (variant).
         Иначе берём строку целиком.
      2. Делим на токены (whitespace).
      3. Для каждого токена пробуем найти в COUNTRY_FLAGS (case-insens).
      4. Первый match — возвращаем флаг. Иначе — пустая строка.

    Примеры:
      region_flag("Apple Gift Card | US")    → "🇺🇸"
      region_flag("Apple Gift Card | TR | 10 TRY") → "🇹🇷"  (TR матчится первым)
      region_flag("US 5 USD")                → "🇺🇸"
      region_flag("Spotify Premium")         → ""  (нет страны)
      region_flag("IDR 6000 Steam Wallet Code") → "🇮🇩"  (IDR матчится)
    """
    if not category_or_variant:
        return ""
    # Разделение «|»: если есть, берём хвост; иначе строку целиком.
    if "|" in category_or_variant:
        _, _, tail = category_or_variant.partition("|")
        text = tail.strip()
    else:
        text = category_or_variant

    # Дополнительная очистка от non-letter: «10 TRY» → ["10", "TRY"]
    # «$5», «USD» — пробуем оба варианта.
    for raw_token in text.replace("|", " ").split():
        token = raw_token.strip("$€₽¥£.,()[]{}").upper()
        if not token:
            continue
        if token in COUNTRY_FLAGS:
            return COUNTRY_FLAGS[token]
    return ""


# ─── Stock indicators ───────────────────────────────────────────────


def stock_bar(in_stock: int, *, cap: int = 10) -> str:
    """
    Графический индикатор наличия товара. 5 «ячеек», заполняются
    пропорционально in_stock/cap.

    Примеры:
      stock_bar(10, cap=10) → "🟩🟩🟩🟩🟩"   (full)
      stock_bar(7,  cap=10) → "🟩🟩🟩🟩⬜"    (4/5)
      stock_bar(3,  cap=10) → "🟩🟩⬜⬜⬜"    (2/5)
      stock_bar(0,  cap=10) → "⬜⬜⬜⬜⬜"     (none)
      stock_bar(50, cap=10) → "🟩🟩🟩🟩🟩"   (capped)

    Используем 🟩 (зелёный квадрат) для «есть» и ⬜ (белый/контурный)
    для «нет». В тёмной теме оба хорошо видны, в светлой — тоже.
    """
    if in_stock <= 0 or cap <= 0:
        return "⬜" * 5
    # Сколько ячеек заполнено (1..5)
    filled = max(1, min(5, round(in_stock * 5 / cap)))
    return "🟩" * filled + "⬜" * (5 - filled)


def stock_status_text(in_stock: int) -> str:
    """
    Текстовый статус для карточки товара.

    Стратегия:
      * >= 10 — «в наличии много», ровный 🟢
      * 5..9  — «в наличии», 🟢
      * 1..4  — «осталось мало», ⚠ + цифра
      * 0     — «нет в наличии», 🚫

    Это короче и читабельнее, чем «📦 В наличии: <b>23</b> шт.» —
    юзеру не важна точная цифра, важен сигнал «можно/нельзя купить».
    """
    if in_stock <= 0:
        return "🚫 <b>Нет в наличии</b>"
    if in_stock >= 10:
        return f"🟢 <b>В наличии</b> (доступно: {in_stock})"
    if in_stock >= 5:
        return f"🟢 <b>В наличии</b> ({in_stock} шт.)"
    return f"⚠ <b>Осталось мало:</b> {in_stock} шт."


# ─── Featured badge ─────────────────────────────────────────────────


FEATURED_BADGES: list[str] = ["🔥", "⭐", "💎"]


def featured_badge(rank: int) -> str:
    """
    Бейдж для топ-N групп каталога. Используем разные эмодзи для
    первых трёх позиций — даёт визуальную иерархию даже внутри
    топа.

    Примеры:
      featured_badge(0) → "🔥"   (топ-1, самое горячее)
      featured_badge(1) → "⭐"   (топ-2)
      featured_badge(2) → "💎"   (топ-3)
      featured_badge(3) → ""     (вне топ-3)

    Если хочется выставить кастомный featured-список — это уже
    через runtime settings. Здесь — авто-fallback «топ по variants».
    """
    if 0 <= rank < len(FEATURED_BADGES):
        return FEATURED_BADGES[rank]
    return ""
