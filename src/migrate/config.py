"""
Конфиг автономной миграции платформы: одна нода + рецепт заполнения
полей формы. Заполняется ОДИН раз на платформу, дальше migrate_auto
прогоняет все её категории автоматически.

Формат (YAML):

    platforms:
      - name: Steam                 # имя для профиля описаний и логов
        ns_grep: "steam wallet"     # какие NS-категории брать (по имени)
        funpay_node: 1086
        markup_percent: 5
        # Поля формы FunPay. Теги {currency}{nominal}{region_ru}{region_en}.
        # 'fields' — одинаковые для всех валют категории:
        fields:
          "fields[currency]": "{currency}"
          "fields[type]": "Подарочная карта"
          "fields[quantity]": "{nominal}"

      - name: Apple
        ns_grep: "apple"
        funpay_node: 1316
        markup_percent: 5
        # 'by_currency' — разный набор полей на валюту (имя поля номинала
        # зависит от валюты). Категории с валютой не из списка пропускаются.
        by_currency:
          USD: { "fields[currency]": "USD", "fields[usd]": "{nominal} USD" }
          BRL: { "fields[currency]": "BRL", "fields[brl]": "{nominal} BRL" }

      - name: PlayStation
        ns_grep: "playstation gift"
        funpay_node: 935
        markup_percent: 5
        by_currency:
          GBP: { "fields[type]": "Карта пополнения", "fields[country]": "Великобритания (GBP)", "fields[gbquantity]": "{nominal} GBP" }
          EUR: { "fields[type]": "Карта пополнения", "fields[country]": "Германия (EUR)", "fields[eurquantity]": "{nominal} EUR" }

Приоритет выбора набора полей для категории:
    by_category[cat_id]  >  by_currency[currency]  >  fields
Если ни один не подошёл (нет рецепта для валюты) — категория пропускается.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class PlatformConfig:
    name: str
    funpay_node: int
    markup_percent: float = 5.0
    ns_grep: str | None = None
    ns_cat_ids: list[int] = field(default_factory=list)
    fields: dict[str, str] | None = None
    by_currency: dict[str, dict[str, str]] = field(default_factory=dict)
    by_category: dict[int, dict[str, str]] = field(default_factory=dict)
    # id существующего лота этой ноды. Если задан — схему тянем через
    # offerEdit?offer=<id>, чтобы получить ВСЕ валютные селекты (Apple
    # рендерит fields[try]/[eur]/... только при открытии лота, а не в
    # пустой форме). Без него валидация номиналов недефолтных валют
    # неполная (полагаемся на серверную проверку FunPay при создании).
    schema_offer: int | None = None


@dataclass
class MigrationConfig:
    platforms: list[PlatformConfig] = field(default_factory=list)


def _coerce_fields(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"Ожидался словарь полей, получил {type(raw).__name__}")
    return {str(k): str(v) for k, v in raw.items()}


def parse_platform(raw: dict[str, Any]) -> PlatformConfig:
    by_currency = {
        str(cur): _coerce_fields(f) or {}
        for cur, f in (raw.get("by_currency") or {}).items()
    }
    by_category = {
        int(cid): _coerce_fields(f) or {}
        for cid, f in (raw.get("by_category") or {}).items()
    }
    return PlatformConfig(
        name=str(raw["name"]),
        funpay_node=int(raw["funpay_node"]),
        markup_percent=float(raw.get("markup_percent", 5.0)),
        ns_grep=(str(raw["ns_grep"]) if raw.get("ns_grep") else None),
        ns_cat_ids=[int(x) for x in (raw.get("ns_cat_ids") or [])],
        fields=_coerce_fields(raw.get("fields")),
        by_currency=by_currency,
        by_category=by_category,
        schema_offer=(int(raw["schema_offer"]) if raw.get("schema_offer") else None),
    )


def load_config(path: str) -> MigrationConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    platforms_raw = data.get("platforms")
    if not isinstance(platforms_raw, list):
        raise ValueError("Конфиг должен содержать список 'platforms'")
    return MigrationConfig(platforms=[parse_platform(p) for p in platforms_raw])


def get_platform(config: MigrationConfig, name: str) -> PlatformConfig | None:
    for p in config.platforms:
        if p.name.strip().lower() == name.strip().lower():
            return p
    return None


def resolve_fields(
    pc: PlatformConfig, category_id: int, currency: str | None
) -> dict[str, str] | None:
    """
    Набор полей формы для категории. None = рецепта нет, категорию
    пропускаем (например валюта, которую раздел FunPay не поддерживает).
    """
    if category_id in pc.by_category:
        return pc.by_category[category_id]
    if currency is not None and currency in pc.by_currency:
        return pc.by_currency[currency]
    if pc.by_currency and pc.fields is None:
        # by_currency задан, но валюты нет в нём и общего fields нет — пропуск
        return None
    return pc.fields
