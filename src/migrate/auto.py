"""
Автономная миграция платформы: один конфиг + один прогон создаёт лоты
по ВСЕМ подходящим категориям раздела.

Что делает migrate_platform:
  1. Берёт NS-категории платформы (по ns_grep / ns_cat_ids).
  2. Оставляет только пригодные (quantity-only) и с услугами в наличии
     (>= min_stock) — «пустышки с 0» отсекаются.
  3. Тянет схему FunPay-ноды один раз.
  4. Для каждой категории: определяет валюту, берёт рецепт полей из
     конфига (by_category > by_currency > fields), строит запись и
     прогоняет через run_category (валидация против схемы + создание
     неактивных лотов + staged-маппинги, с идемпотентностью по
     ns_service_id — уже созданное пропускается).
  5. Агрегирует итог.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.migrate.catalog import build_skeleton_entry, classify_category
from src.migrate.config import PlatformConfig, resolve_fields
from src.migrate.loader import MigrationEntry, MigrationService
from src.migrate.profiles import profile_for
from src.migrate.runner import RunResult, run_category
from src.migrate.skeleton_yaml import (
    DEFAULT_DESC_EN,
    DEFAULT_DESC_RU,
    DEFAULT_SUMMARY_EN,
    DEFAULT_SUMMARY_RU,
)


@dataclass
class CategoryOutcome:
    category_id: int
    category_name: str
    currency: str | None
    result: RunResult | None
    skipped_reason: str | None = None


@dataclass
class AutoResult:
    outcomes: list[CategoryOutcome] = field(default_factory=list)

    @property
    def total_created(self) -> int:
        return sum(len(o.result.created) for o in self.outcomes if o.result)

    @property
    def total_already(self) -> int:
        return sum(
            len(o.result.skipped_already_mapped) for o in self.outcomes if o.result
        )

    @property
    def total_unsupported(self) -> int:
        return sum(
            len(o.result.skipped_unsupported) for o in self.outcomes if o.result
        )

    @property
    def total_errors(self) -> int:
        return sum(len(o.result.errors) for o in self.outcomes if o.result)


def _entry_for_category(
    category: Any,
    pc: PlatformConfig,
    fields: dict[str, str],
    profiles: dict[str, dict[str, str]],
    *,
    min_stock: int,
) -> MigrationEntry:
    skel = build_skeleton_entry(category)
    prof = profile_for(profiles, pc.name)
    services = [
        MigrationService(
            service_id=s.service_id,
            nominal=s.nominal,
            price_usd=s.price_usd,
            in_stock=s.in_stock,
        )
        for s in skel.services
        if s.in_stock >= min_stock
    ]
    return MigrationEntry(
        ns_category_id=skel.ns_category_id,
        ns_category_name=skel.ns_category_name,
        platform=skel.platform,
        currency=skel.currency,
        region_code=skel.region_code,
        region_ru=skel.region_ru,
        region_en=skel.region_en,
        funpay_node=pc.funpay_node,
        markup_percent=pc.markup_percent,
        funpay_fields=fields,
        summary_ru=prof.get("summary_ru", DEFAULT_SUMMARY_RU),
        summary_en=prof.get("summary_en", DEFAULT_SUMMARY_EN),
        desc_ru=prof.get("desc_ru", DEFAULT_DESC_RU),
        desc_en=prof.get("desc_en", DEFAULT_DESC_EN),
        services=services,
    )


def _select_categories(stock: Any, pc: PlatformConfig) -> list[Any]:
    cats = stock.categories
    if pc.ns_cat_ids:
        wanted = set(pc.ns_cat_ids)
        return [c for c in cats if c.category_id in wanted]
    if pc.ns_grep:
        needle = pc.ns_grep.lower()
        return [c for c in cats if needle in (c.category_name or "").lower()]
    return []


async def migrate_platform(
    pc: PlatformConfig,
    *,
    admin: Any,
    schema: dict[str, Any],
    fx_rate: float,
    stock: Any,
    profiles: dict[str, dict[str, str]] | None = None,
    min_stock: int = 1,
    limit_categories: int | None = None,
    activate: bool = False,
    dry_run: bool = True,
) -> AutoResult:
    profiles = profiles or {}
    result = AutoResult()

    cats = _select_categories(stock, pc)
    logger.info(
        f"=== Платформа {pc.name}: node {pc.funpay_node}, найдено категорий "
        f"{len(cats)}" + (" [DRY-RUN]" if dry_run else "") + " ==="
    )

    processed = 0
    for category in cats:
        elig = classify_category(category)
        if not elig.eligible:
            result.outcomes.append(CategoryOutcome(
                category.category_id, category.category_name, None, None,
                skipped_reason=f"непригодна: {elig.reason}",
            ))
            continue

        skel = build_skeleton_entry(category)
        fields = resolve_fields(pc, category.category_id, skel.currency)
        if not fields:
            result.outcomes.append(CategoryOutcome(
                category.category_id, category.category_name, skel.currency, None,
                skipped_reason=f"нет рецепта полей для валюты {skel.currency!r}",
            ))
            logger.warning(
                f"  [{category.category_id}] {category.category_name}: "
                f"пропуск — нет рецепта для валюты {skel.currency!r}"
            )
            continue

        entry = _entry_for_category(category, pc, fields, profiles, min_stock=min_stock)
        if not entry.in_stock_services():
            result.outcomes.append(CategoryOutcome(
                category.category_id, category.category_name, skel.currency, None,
                skipped_reason=f"нет услуг со стоком >= {min_stock}",
            ))
            continue

        run_res = await run_category(
            entry, admin, schema, fx_rate,
            activate=activate, dry_run=dry_run,
        )
        result.outcomes.append(CategoryOutcome(
            category.category_id, category.category_name, skel.currency, run_res,
        ))
        processed += 1
        if limit_categories is not None and processed >= limit_categories:
            logger.info(f"Достигнут лимит категорий ({limit_categories}).")
            break

    return result
