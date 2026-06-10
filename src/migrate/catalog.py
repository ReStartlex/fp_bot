"""
Классификация каталога NS и парсинг названий — фундамент миграции NS→FunPay.

Два главных вывода из реального каталога (512 категорий, 2206 услуг):

1. **Пригодность определяется order-fields категории.**
   - `quantity` (и только он) — чистая выдача кодов: бот покупает N штук,
     получает пины, отдаёт. МИГРИРУЕМО на автовыдачу FunPay.
   - `account_number` / `server_id` / `zone_id` / `account` / `amount` /
     `region` / `sub_id` / ... — пополнения, которым нужен аккаунт
     покупателя. Через автовыдачу FunPay невыполнимо → НЕ мигрируем.

2. **Регион и валюта зафиксированы на уровне NS-категории**, меняется
   только номинал. Поэтому регион/валюту объявляем один раз на категорию,
   а из service_name парсим только число номинала.

Парсер устойчив к разным форматам service_name даже внутри одного бренда:
   - "Apple Gift Card | USA | 2 USD"   (регион/номинал через |)
   - "AED 50 Apple gift card"          (валюта-префикс)
   - "EUR 15 Apple gift card"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.ns.models import Category, Service


# Единственный order-field, пригодный для автовыдачи FunPay. Любой другой
# (account_number, server_id, zone_id, account, amount, region, sub_id,
# friendLink, giftName, ...) означает пополнение/гифт с вводом данных
# покупателя — такие категории миграции НЕ подлежат.
ELIGIBLE_ORDER_FIELDS: frozenset[str] = frozenset({"quantity"})


# Коды валют, которые встречаются в названиях NS (для детекта валюты услуги).
KNOWN_CURRENCIES: frozenset[str] = frozenset({
    "USD", "EUR", "TRY", "BRL", "AED", "CAD", "GBP", "INR", "JPY", "PLN",
    "AUD", "IDR", "MXN", "ZAR", "KZT", "UAH", "RUB", "HKD", "SGD", "MYR",
    "THB", "PHP", "VND", "KRW", "TWD", "QAR", "SAR", "KWD", "OMR", "BHD",
    "CZK", "HUF", "RON", "HRK", "NZD", "CHF", "CLP", "COP", "ARS", "NGN",
})


# Региональные коды NS → (русское, английское) название. Берётся из
# суффикса category_name ("Apple | USA" → USA). Неизвестные коды
# оставляем None — пользователь дозаполнит в скелете.
REGION_NAMES: dict[str, tuple[str, str]] = {
    "USA": ("США", "USA"), "US": ("США", "US"),
    "UK": ("Великобритания", "UK"), "GB": ("Великобритания", "UK"),
    "EU": ("Европа", "Europe"),
    "AE": ("ОАЭ", "UAE"), "AU": ("Австралия", "Australia"),
    "BE": ("Бельгия", "Belgium"), "BR": ("Бразилия", "Brazil"),
    "CA": ("Канада", "Canada"), "DE": ("Германия", "Germany"),
    "FR": ("Франция", "France"), "IE": ("Ирландия", "Ireland"),
    "IN": ("Индия", "India"), "IT": ("Италия", "Italy"),
    "JP": ("Япония", "Japan"), "PL": ("Польша", "Poland"),
    "PT": ("Португалия", "Portugal"), "TR": ("Турция", "Turkey"),
    "RU": ("Россия", "Russia"), "MX": ("Мексика", "Mexico"),
    "ID": ("Индонезия", "Indonesia"), "ZA": ("ЮАР", "South Africa"),
    "SA": ("Саудовская Аравия", "Saudi Arabia"), "TW": ("Тайвань", "Taiwan"),
    "HK": ("Гонконг", "Hong Kong"), "SG": ("Сингапур", "Singapore"),
    "MY": ("Малайзия", "Malaysia"), "TH": ("Таиланд", "Thailand"),
    "PH": ("Филиппины", "Philippines"), "KR": ("Корея", "Korea"),
    "AR": ("Аргентина", "Argentina"), "CO": ("Колумбия", "Colombia"),
    "NZ": ("Новая Зеландия", "New Zealand"), "CZ": ("Чехия", "Czechia"),
    "HU": ("Венгрия", "Hungary"), "RO": ("Румыния", "Romania"),
    "NL": ("Нидерланды", "Netherlands"), "ES": ("Испания", "Spain"),
    "AT": ("Австрия", "Austria"), "FI": ("Финляндия", "Finland"),
    "KW": ("Кувейт", "Kuwait"), "QA": ("Катар", "Qatar"),
    "OM": ("Оман", "Oman"), "BH": ("Бахрейн", "Bahrain"),
    "VN": ("Вьетнам", "Vietnam"),
}


@dataclass
class CategoryEligibility:
    category_id: int
    category_name: str
    order_fields: list[str]
    eligible: bool
    reason: str
    in_stock_services: int
    total_services: int


def classify_category(category: Category) -> CategoryEligibility:
    """Определяет, пригодна ли NS-категория для миграции на автовыдачу FunPay."""
    order_fields = [f.key for f in category.fields]
    in_stock = sum(1 for s in category.services if s.in_stock > 0)

    fields_set = set(order_fields)
    if not fields_set:
        eligible, reason = False, "нет order-fields (нечего покупать)"
    elif fields_set <= ELIGIBLE_ORDER_FIELDS:
        if in_stock == 0:
            eligible, reason = False, "нет услуг в наличии"
        else:
            eligible, reason = True, "ok (quantity-only, есть наличие)"
    else:
        extra = sorted(fields_set - ELIGIBLE_ORDER_FIELDS)
        eligible = False
        reason = f"требует ввод покупателя: {', '.join(extra)} (пополнение)"

    return CategoryEligibility(
        category_id=category.category_id,
        category_name=category.category_name,
        order_fields=order_fields,
        eligible=eligible,
        reason=reason,
        in_stock_services=in_stock,
        total_services=len(category.services),
    )


def detect_currency(services: list[Service]) -> str | None:
    """
    Валюта категории: самый частый код валюты в названиях услуг.

    NS пишет валюту по-разному ("2 USD", "AED 50", "EUR 15"), но код
    валюты всегда присутствует как отдельный токен. Берём моду по всем
    услугам — устойчиво к единичным опечаткам.
    """
    counts: dict[str, int] = {}
    for s in services:
        tokens = set(re.findall(r"[A-Za-z]{3}", s.service_name.upper()))
        for tok in tokens & KNOWN_CURRENCIES:
            counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def detect_region(category_name: str) -> tuple[str | None, str | None, str | None]:
    """
    Регион из суффикса имени категории.

    "Apple | USA" → ("USA", "США", "USA")
    "Battle.net Gift Card | CA" → ("CA", "Канада", "Canada")
    Возвращает (код, ru, en). Код может быть, а имена — None (нет в словаре).
    Если суффикс не похож на код (game без региона) — (None, None, None).
    """
    parts = [p.strip() for p in category_name.split("|")]
    if len(parts) < 2:
        return (None, None, None)
    candidate = parts[-1].upper()
    # код региона — короткий буквенный токен (2-3 буквы) или известное имя
    if candidate in REGION_NAMES:
        ru, en = REGION_NAMES[candidate]
        return (candidate, ru, en)
    if re.fullmatch(r"[A-Z]{2,3}", candidate):
        return (candidate, None, None)
    return (None, None, None)


def detect_platform(category_name: str) -> str:
    """Платформа/бренд — первый сегмент имени категории до '|'."""
    return category_name.split("|")[0].strip()


def extract_nominal(service_name: str, currency: str | None) -> int | None:
    """
    Номинал (число) из service_name.

    Стратегия по приоритету:
      1. число рядом с кодом валюты: "2 USD" / "USD 2" / "AED 50";
      2. если валюта неизвестна — первое «осмысленное» целое в названии.

    Возвращает int либо None, если число не нашлось.
    """
    name = service_name
    if currency:
        cur = re.escape(currency)
        m = re.search(rf"(\d[\d.,]*)\s*{cur}\b", name, re.IGNORECASE)
        if m is None:
            m = re.search(rf"\b{cur}\s*(\d[\d.,]*)", name, re.IGNORECASE)
        if m is not None:
            return _to_int(m.group(1))
    # fallback: первое целое (без разделителей)
    m = re.search(r"\b(\d[\d.,]*)\b", name)
    if m is not None:
        return _to_int(m.group(1))
    return None


def _to_int(raw: str) -> int | None:
    cleaned = raw.replace(",", "").replace(".", "")
    try:
        return int(cleaned)
    except ValueError:
        return None


@dataclass
class SkeletonService:
    service_id: int
    service_name: str
    price_usd: float
    in_stock: int
    nominal: int | None


@dataclass
class SkeletonEntry:
    """Заготовка YAML-записи для одной NS-категории (только NS-сторона)."""
    ns_category_id: int
    ns_category_name: str
    platform: str
    region_code: str | None
    region_ru: str | None
    region_en: str | None
    currency: str | None
    services: list[SkeletonService] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)


def build_skeleton_entry(category: Category, *, only_in_stock: bool = True) -> SkeletonEntry:
    """
    Строит заготовку записи для категории: выводит всё, что можно из NS
    (платформа, регион, валюта, номиналы), оставляя FunPay-node и
    шаблоны на дозаполнение человеку.
    """
    services_src = [
        s for s in category.services
        if (s.in_stock > 0 or not only_in_stock)
    ]
    currency = detect_currency(services_src or category.services)
    region_code, region_ru, region_en = detect_region(category.category_name)
    platform = detect_platform(category.category_name)

    warnings: list[str] = []
    skeleton_services: list[SkeletonService] = []
    seen_nominals: dict[int, int] = {}
    for s in services_src:
        nominal = extract_nominal(s.service_name, currency)
        if nominal is None:
            warnings.append(f"не распознан номинал: svc_id={s.service_id} «{s.service_name}»")
        else:
            seen_nominals[nominal] = seen_nominals.get(nominal, 0) + 1
        skeleton_services.append(SkeletonService(
            service_id=s.service_id,
            service_name=s.service_name,
            price_usd=s.price,
            in_stock=s.in_stock,
            nominal=nominal,
        ))

    dup = [n for n, c in seen_nominals.items() if c > 1]
    if dup:
        warnings.append(f"повторяющиеся номиналы {sorted(dup)} — проверь, не разные ли это товары")
    if currency is None:
        warnings.append("не определена валюта категории")
    if region_code and region_ru is None:
        warnings.append(f"регион '{region_code}' нет в словаре — заполни region_ru/region_en вручную")

    return SkeletonEntry(
        ns_category_id=category.category_id,
        ns_category_name=category.category_name,
        platform=platform,
        region_code=region_code,
        region_ru=region_ru,
        region_en=region_en,
        currency=currency,
        services=skeleton_services,
        parse_warnings=warnings,
    )
