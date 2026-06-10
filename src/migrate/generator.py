"""
Генерация payload'ов лотов FunPay из записи таблицы соответствий +
валидация против схемы FunPay-раздела.

Принцип безопасности: ни одно значение селекта не угадывается. Для
каждой услуги подставляем теги в funpay_fields/шаблоны и СВЕРЯЕМ
получившиеся значения select-полей с реальными опциями схемы раздела.
Если значения нет среди опций — услуга помечается неподдерживаемой и
НЕ создаётся (это тот самый случай «в разделе нет такой валюты/номинала»).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.migrate.loader import MigrationEntry, MigrationService


SUBST_TAGS = ("platform", "nominal", "currency", "region_ru", "region_en")


def substitute(template: str, *, entry: MigrationEntry, service: MigrationService) -> str:
    """Подставляет теги {platform}{nominal}{currency}{region_ru}{region_en}."""
    ctx = {
        "platform": entry.platform,
        "nominal": "" if service.nominal is None else str(service.nominal),
        "currency": entry.currency or "",
        "region_ru": entry.region_ru or "",
        "region_en": entry.region_en or "",
    }
    out = template
    for tag, val in ctx.items():
        out = out.replace("{" + tag + "}", val)
    return out


def compute_price_rub(price_usd: float, markup_percent: float, fx_rate: float) -> float:
    """Стартовая цена лота: NS USD × (1+markup) × курс. Округление до 2 знаков.

    Это лишь начальное значение — после создания маппинга sync_stock
    каждые ~30с пересчитает цену по своей логике. Главное, чтобы цена
    была валидной для FunPay (в допустимом диапазоне).
    """
    return round(price_usd * (1.0 + markup_percent / 100.0) * fx_rate, 2)


# ── индекс схемы раздела ──

@dataclass
class NodeSchemaIndex:
    """Удобный индекс по схеме формы (из funpay_node_schema.parse_form_schema)."""
    select_options: dict[str, set[str]]  # имя поля → допустимые value+text
    select_names: set[str]
    all_field_names: set[str]

    @classmethod
    def from_schema(cls, schema: dict[str, Any]) -> "NodeSchemaIndex":
        select_options: dict[str, set[str]] = {}
        select_names: set[str] = set()
        all_names: set[str] = set()
        for sel in schema.get("selects", []):
            name = sel["name"]
            select_names.add(name)
            all_names.add(name)
            opts: set[str] = set()
            for o in sel.get("options", []):
                if o.get("value", "") != "":
                    opts.add(str(o["value"]))
                if o.get("text", "") != "":
                    opts.add(str(o["text"]))
            select_options[name] = opts
        for inp in schema.get("inputs", []):
            all_names.add(inp["name"])
        for ta in schema.get("textareas", []):
            all_names.add(ta["name"])
        return cls(
            select_options=select_options,
            select_names=select_names,
            all_field_names=all_names,
        )


@dataclass
class ServiceValidation:
    service: MigrationService
    ok: bool
    reasons: list[str] = field(default_factory=list)
    resolved_fields: dict[str, str] = field(default_factory=dict)
    price_rub: float | None = None


@dataclass
class EntryValidation:
    field_name_warnings: list[str] = field(default_factory=list)
    services: list[ServiceValidation] = field(default_factory=list)

    @property
    def ok_services(self) -> list[ServiceValidation]:
        return [s for s in self.services if s.ok]

    @property
    def bad_services(self) -> list[ServiceValidation]:
        return [s for s in self.services if not s.ok]


def validate_entry_against_schema(
    entry: MigrationEntry,
    schema: dict[str, Any],
    fx_rate: float,
    *,
    only_in_stock: bool = True,
) -> EntryValidation:
    """
    Сверяет запись с реальной схемой FunPay-раздела.

    Для каждой услуги:
      - подставляет теги в funpay_fields;
      - если поле — select, проверяет, что значение есть среди опций;
      - считает стартовую цену.
    Услуга ok=True только если все её select-значения валидны.
    """
    idx = NodeSchemaIndex.from_schema(schema)
    result = EntryValidation()

    # Предупреждения об именах полей, которых нет в форме раздела (опечатки).
    for fname in entry.funpay_fields:
        if fname not in idx.all_field_names:
            result.field_name_warnings.append(
                f"поле '{fname}' отсутствует в форме раздела "
                f"(опечатка? проверь funpay_node_schema)"
            )

    services = entry.in_stock_services() if only_in_stock else entry.services
    for svc in services:
        reasons: list[str] = []
        resolved: dict[str, str] = {}
        for fname, ftemplate in entry.funpay_fields.items():
            value = substitute(ftemplate, entry=entry, service=svc)
            resolved[fname] = value
            if fname in idx.select_names:
                opts = idx.select_options.get(fname, set())
                if value not in opts:
                    reasons.append(
                        f"значение '{value}' поля '{fname}' нет среди опций "
                        f"раздела (доступно: {_preview_opts(opts)})"
                    )
        price = compute_price_rub(svc.price_usd, entry.markup_percent, fx_rate)
        result.services.append(ServiceValidation(
            service=svc,
            ok=not reasons,
            reasons=reasons,
            resolved_fields=resolved,
            price_rub=price,
        ))
    return result


def _preview_opts(opts: set[str], limit: int = 8) -> str:
    items = sorted(opts)[:limit]
    more = "…" if len(opts) > limit else ""
    return ", ".join(items) + more


def build_creation_fields(
    entry: MigrationEntry,
    service: MigrationService,
    fx_rate: float,
) -> dict[str, str]:
    """
    Полный набор override-полей для формы создания лота FunPay
    (накладывается поверх пустой формы offer_id=0). Лот — неактивный.

    Включает summary/desc на обоих языках. Поля, которых нет в конкретном
    разделе (напр. summary у Steam Wallet), отфильтрует
    filter_fields_to_schema перед отправкой.
    """
    fields: dict[str, str] = {}
    for fname, ftemplate in entry.funpay_fields.items():
        fields[fname] = substitute(ftemplate, entry=entry, service=service)
    fields["fields[summary][ru]"] = substitute(entry.summary_ru, entry=entry, service=service)
    fields["fields[summary][en]"] = substitute(entry.summary_en, entry=entry, service=service)
    fields["fields[desc][ru]"] = substitute(entry.desc_ru, entry=entry, service=service)
    fields["fields[desc][en]"] = substitute(entry.desc_en, entry=entry, service=service)
    return fields


def filter_fields_to_schema(
    fields: dict[str, str], idx: NodeSchemaIndex
) -> tuple[dict[str, str], list[str]]:
    """
    Оставляет только те поля, что реально есть в форме раздела.

    Зачем: разделы различаются составом полей. Напр. Steam Wallet (node
    1086) не имеет краткого описания (fields[summary]); PlayStation —
    свой набор. Слать поле, которого в форме нет, незачем — фильтруем.

    Возвращает (оставленные, отброшенные_имена).
    """
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for name, value in fields.items():
        if name in idx.all_field_names:
            kept[name] = value
        else:
            dropped.append(name)
    return kept, dropped
