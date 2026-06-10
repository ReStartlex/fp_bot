"""
Автономная миграция платформы NS→FunPay по конфигу.

Один прогон создаёт лоты по ВСЕМ подходящим категориям платформы:
пропускает непригодные, пустышки (0 в наличии) и уже созданные
(идемпотентность по ns_service_id). По умолчанию DRY-RUN.

Запуск:
    # план по Steam (ничего не создаёт)
    ./.venv/bin/python -m src.tools.migrate_auto /root/migration.yaml \
        --platform Steam --profiles /root/profiles.yaml

    # реально создать (неактивные лоты + staged-маппинги)
    ./.venv/bin/python -m src.tools.migrate_auto /root/migration.yaml \
        --platform Steam --profiles /root/profiles.yaml --yes

    # сразу активными (в продажу) и пропуская стоки < 3
    ./.venv/bin/python -m src.tools.migrate_auto /root/migration.yaml \
        --platform Steam --yes --activate --min-stock 3
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.config import get_settings
from src.funpay.admin_http import FunPayAdminClient
from src.logging_setup import setup_logging
from src.migrate.auto import migrate_platform
from src.migrate.config import get_platform, load_config
from src.sync.fx import get_usd_rub_rate
from src.tools.funpay_node_schema import parse_form_schema
from src.ns import NSClient


def _build_admin(settings) -> FunPayAdminClient:
    return FunPayAdminClient(
        golden_key=settings.funpay_golden_key.get_secret_value(),
        phpsessid=(
            settings.funpay_phpsessid.get_secret_value()
            if settings.funpay_phpsessid else None
        ),
    )


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Автономная миграция платформы NS→FunPay")
    parser.add_argument("config_path", help="YAML-конфиг миграции (platforms)")
    parser.add_argument("--platform", required=True, help="имя платформы из конфига")
    parser.add_argument("--profiles", default=None, help="YAML-профили описаний")
    parser.add_argument("--yes", action="store_true", help="реально создавать (иначе dry-run)")
    parser.add_argument("--activate", action="store_true", help="сразу активные лоты")
    parser.add_argument("--min-stock", type=int, default=1, help="мин. сток услуги (default 1)")
    parser.add_argument(
        "--limit-categories", type=int, default=None,
        help="обработать не больше N категорий (для пробной волны)",
    )
    args = parser.parse_args()

    config = load_config(args.config_path)
    pc = get_platform(config, args.platform)
    if pc is None:
        logger.error(
            f"Платформа {args.platform!r} не найдена. Есть: "
            f"{[p.name for p in config.platforms]}"
        )
        return 1

    profiles = {}
    if args.profiles:
        from src.migrate.profiles import load_profiles
        profiles = load_profiles(args.profiles)

    settings = get_settings()
    if args.yes and not settings.enable_real_actions:
        logger.error("ENABLE_REAL_ACTIONS=false — реальное создание запрещено.")
        return 1

    async with NSClient() as ns:
        await ns.login()
        stock = await ns.get_stock()

    fx_rate = await get_usd_rub_rate(settings)
    admin = _build_admin(settings)

    # Если задан schema_offer — тянем форму существующего лота: так нода
    # отдаёт ВСЕ валютные селекты (Apple показывает fields[try]/[eur]/...
    # только при открытии лота). Иначе — пустая форма создания.
    if pc.schema_offer:
        url = (
            f"{admin.BASE}/lots/offerEdit?node={pc.funpay_node}"
            f"&offer={pc.schema_offer}&location=offer"
        )
    else:
        url = f"{admin.BASE}/lots/offerEdit?node={pc.funpay_node}"
    r = await asyncio.to_thread(admin._sync_get, url)
    schema = parse_form_schema(r.text, url)
    logger.info(f"Схема ноды {pc.funpay_node} загружена (schema_offer={pc.schema_offer})")

    if args.activate and args.yes:
        logger.warning("⚠ --activate: лоты создаются СРАЗУ активными (в продажу).")

    result = await migrate_platform(
        pc, admin=admin, schema=schema, fx_rate=fx_rate, stock=stock,
        profiles=profiles, min_stock=args.min_stock,
        limit_categories=args.limit_categories,
        activate=args.activate, dry_run=not args.yes,
    )

    logger.info("=" * 70)
    for o in result.outcomes:
        if o.result is None:
            logger.info(f"  ⏭ [{o.category_id}] {o.category_name}: {o.skipped_reason}")
        else:
            logger.info(
                f"  [{o.category_id}] {o.category_name} ({o.currency}): "
                f"создано {len(o.result.created)}, уже было "
                f"{len(o.result.skipped_already_mapped)}, неподдерж. "
                f"{len(o.result.skipped_unsupported)}, ошибок {len(o.result.errors)}"
            )
    logger.info("-" * 70)
    logger.info(
        f"ИТОГО {pc.name}: создано {result.total_created}, уже было "
        f"{result.total_already}, неподдерж. {result.total_unsupported}, "
        f"ошибок {result.total_errors}"
    )
    if not args.yes:
        logger.info("Это был DRY-RUN. Добавь --yes для реального создания.")
    elif result.total_created and not args.activate:
        logger.info(
            "Лоты созданы НЕАКТИВНЫМИ, маппинги staged. Включи маппинги "
            "(Telegram /mappings ▶) — sync активирует их."
        )
    return 0 if result.total_errors == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
