"""
Загрузка и валидация YAML-таблицы соответствий NS→FunPay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from src.migrate.skeleton_yaml import TODO


@dataclass
class MigrationService:
    service_id: int
    nominal: int | None
    price_usd: float
    in_stock: int


@dataclass
class MigrationEntry:
    ns_category_id: int
    ns_category_name: str
    platform: str
    currency: str | None
    region_code: str | None
    region_ru: str | None
    region_en: str | None
    funpay_node: int | None
    markup_percent: float
    funpay_fields: dict[str, str]
    summary_ru: str
    summary_en: str
    desc_ru: str
    desc_en: str
    services: list[MigrationService] = field(default_factory=list)

    def todo_problems(self) -> list[str]:
        """
        Список незаполненных/проблемных мест. Пустой список = запись
        готова к валидации/созданию.
        """
        problems: list[str] = []
        if not isinstance(self.funpay_node, int):
            problems.append(f"funpay_node не заполнен (сейчас: {self.funpay_node!r})")
        if not self.funpay_fields:
            problems.append("funpay_fields пуст")
        for k, v in self.funpay_fields.items():
            if TODO in str(k) or TODO in str(v):
                problems.append(f"funpay_fields содержит {TODO}: {k!r}: {v!r}")
        for tagname, val in (
            ("region_ru", self.region_ru),
            ("region_en", self.region_en),
            ("currency", self.currency),
        ):
            if val is None or TODO in str(val):
                problems.append(f"{tagname} не заполнен ({val!r})")
        return problems

    def in_stock_services(self) -> list[MigrationService]:
        return [s for s in self.services if s.in_stock > 0]


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_entry(raw: dict[str, Any]) -> MigrationEntry:
    services = []
    for s in raw.get("services", []) or []:
        services.append(MigrationService(
            service_id=int(s["service_id"]),
            nominal=_coerce_int(s.get("nominal")),
            price_usd=float(s.get("price_usd", 0.0)),
            in_stock=int(s.get("in_stock", 0)),
        ))
    return MigrationEntry(
        ns_category_id=int(raw["ns_category_id"]),
        ns_category_name=str(raw.get("ns_category_name", "")),
        platform=str(raw.get("platform", "")),
        currency=(str(raw["currency"]) if raw.get("currency") is not None else None),
        region_code=(str(raw["region_code"]) if raw.get("region_code") else None),
        region_ru=(str(raw["region_ru"]) if raw.get("region_ru") is not None else None),
        region_en=(str(raw["region_en"]) if raw.get("region_en") is not None else None),
        funpay_node=_coerce_int(raw.get("funpay_node")),
        markup_percent=float(raw.get("markup_percent", 0.0)),
        funpay_fields={str(k): str(v) for k, v in (raw.get("funpay_fields") or {}).items()},
        summary_ru=str(raw.get("summary_ru", "")).rstrip("\n"),
        summary_en=str(raw.get("summary_en", "")).rstrip("\n"),
        desc_ru=str(raw.get("desc_ru", "")).rstrip("\n"),
        desc_en=str(raw.get("desc_en", "")).rstrip("\n"),
        services=services,
    )


def load_entries(path: str) -> list[MigrationEntry]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        raise ValueError(f"Ожидался список категорий в {path}, получил {type(data).__name__}")
    return [parse_entry(item) for item in data]


def find_entry(entries: list[MigrationEntry], category_id: int) -> MigrationEntry | None:
    for e in entries:
        if e.ns_category_id == category_id:
            return e
    return None
