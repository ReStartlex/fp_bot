"""
Создание лотов FunPay по записи миграции + запись маппингов NS↔FunPay.

По умолчанию — DRY-RUN (показывает план, ничего не создаёт). Реальное
создание включается флагом --yes. Лоты создаются НЕАКТИВНЫМИ и маппинги
staged (enabled=False), пока не добавишь --activate.

Запуск:
    # план (ничего не создаёт)
    ./.venv/bin/python -m src.tools.migrate_run /root/migrate_apple.yaml --category 4 \
        --funpay-node 1316 \
        --set-field "fields[currency]=USD" --set-field "fields[usd]={nominal} USD" \
        --limit 3

    # реально создать 3 неактивных лота + staged-маппинги
    ./.venv/bin/python -m src.tools.migrate_run /root/migrate_apple.yaml --category 4 \
        --funpay-node 1316 \
        --set-field "fields[currency]=USD" --set-field "fields[usd]={nominal} USD" \
        --limit 3 --yes

После проверки лотов включи маппинги (Telegram /mappings ▶) — sync_stock
выставит цену/сток и активирует лоты. Либо сразу --activate (в продажу).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.config import get_settings
from src.funpay.admin_http import FunPayAdminClient
from src.logging_setup import setup_logging
from src.migrate.loader import load_entries
from src.migrate.runner import run_category
from src.sync.fx import get_usd_rub_rate
from src.tools.funpay_node_schema import parse_form_schema


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
    parser = argparse.ArgumentParser(
        description="Создание лотов FunPay по таблице миграции + маппинги"
    )
    parser.add_argument("yaml_path")
    parser.add_argument("--category", type=int, required=True, help="ns_category_id")
    parser.add_argument("--limit", type=int, default=None, help="макс. лотов за прогон")
    parser.add_argument(
        "--yes", action="store_true",
        help="реально создавать (без флага — dry-run)",
    )
    parser.add_argument(
        "--activate", action="store_true",
        help="сразу активные лоты + enabled-маппинги (по умолчанию staged)",
    )
    # те же оверрайды, что у migrate_validate
    parser.add_argument("--funpay-node", type=int, default=None)
    parser.add_argument("--set-field", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--markup", type=float, default=None)
    args = parser.parse_args()

    entries = load_entries(args.yaml_path)
    entries = [e for e in entries if e.ns_category_id == args.category]
    if not entries:
        logger.error(f"Категория {args.category} не найдена в {args.yaml_path}")
        return 1
    entry = entries[0]

    # оверрайды
    overrides_fields: dict[str, str] = {}
    for raw in args.set_field:
        if "=" not in raw:
            logger.error(f"--set-field ожидает NAME=VALUE: {raw!r}")
            return 1
        name, _, value = raw.partition("=")
        overrides_fields[name.strip()] = value
    if args.funpay_node is not None:
        entry.funpay_node = args.funpay_node
    if overrides_fields:
        entry.funpay_fields = overrides_fields
    if args.markup is not None:
        entry.markup_percent = args.markup

    problems = entry.todo_problems()
    if problems:
        logger.error("Запись не готова — заполни (или передай оверрайдами):")
        for p in problems:
            logger.error(f"  • {p}")
        return 1
    if not entry.in_stock_services():
        logger.error("Нет услуг в наличии — создавать нечего.")
        return 1

    settings = get_settings()
    if args.yes and not settings.enable_real_actions:
        logger.error("ENABLE_REAL_ACTIONS=false — реальное создание запрещено.")
        return 1

    fx_rate = await get_usd_rub_rate(settings)
    admin = _build_admin(settings)

    url = f"{admin.BASE}/lots/offerEdit?node={entry.funpay_node}"
    r = await asyncio.to_thread(admin._sync_get, url)
    schema = parse_form_schema(r.text, url)

    if args.activate and args.yes:
        logger.warning(
            "⚠ --activate: лоты создаются СРАЗУ активными и попадут в продажу. "
            "Убедись, что цены/шаблоны верны."
        )

    result = await run_category(
        entry, admin, schema, fx_rate,
        limit=args.limit,
        activate=args.activate,
        dry_run=not args.yes,
    )

    logger.info("=" * 70)
    logger.info(
        f"Итог: создано {len(result.created)}, "
        f"уже было {len(result.skipped_already_mapped)}, "
        f"неподдерж. {len(result.skipped_unsupported)}, "
        f"ошибок {len(result.errors)}"
    )
    for c in result.created:
        logger.info(f"  lot {c.funpay_lot_id} ← ns {c.ns_service_id} (номинал {c.nominal})")
    if not args.yes:
        logger.info("Это был DRY-RUN. Добавь --yes для реального создания.")
    elif result.created and not args.activate:
        logger.info(
            "Лоты созданы НЕАКТИВНЫМИ, маппинги staged. Проверь лоты на FunPay, "
            "затем включи маппинги (Telegram /mappings ▶) — sync активирует их."
        )
    return 0 if not result.errors else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
