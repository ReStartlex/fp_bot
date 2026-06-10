"""
Дамп каталога NS.gifts (read-only) — для проектирования миграции NS→FunPay.

Показывает реальные строки category_name / service_name, цены, валюты и
остатки. По ним строится таблица соответствий NS-категория → FunPay-node
и правила извлечения номинала/региона/валюты из названий.

Запуск:
    ./.venv/bin/python -m src.tools.ns_catalog_dump --categories
        — только список категорий (id, имя, сколько услуг, сколько в наличии)

    ./.venv/bin/python -m src.tools.ns_catalog_dump --grep apple
        — услуги категорий, в названии которых есть «apple»

    ./.venv/bin/python -m src.tools.ns_catalog_dump --cat-id 12 --in-stock
        — услуги категории 12, только с остатком > 0

    ./.venv/bin/python -m src.tools.ns_catalog_dump --grep battle --json-out /root/ns_battle.json
        — выгрузить в JSON для importer'а
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from loguru import logger

from src.logging_setup import setup_logging
from src.ns import NSClient


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Дамп каталога NS.gifts (read-only)")
    parser.add_argument(
        "--categories", action="store_true",
        help="только список категорий (без услуг)",
    )
    parser.add_argument(
        "--grep", type=str, default=None,
        help="фильтр категорий по подстроке в имени (case-insensitive)",
    )
    parser.add_argument(
        "--cat-id", type=int, default=None, help="только категория с этим id",
    )
    parser.add_argument(
        "--in-stock", action="store_true",
        help="показывать только услуги с остатком > 0",
    )
    parser.add_argument(
        "--json-out", type=str, default=None, help="сохранить отфильтрованное в JSON",
    )
    args = parser.parse_args()

    async with NSClient() as ns:
        await ns.login()
        stock = await ns.get_stock()

    cats = stock.categories
    if args.grep:
        needle = args.grep.lower()
        cats = [c for c in cats if needle in (c.category_name or "").lower()]
    if args.cat_id is not None:
        cats = [c for c in cats if c.category_id == args.cat_id]

    total_services = sum(len(c.services) for c in cats)
    logger.info("=" * 70)
    logger.info(
        f"NS каталог: {len(cats)} категорий (из {len(stock.categories)}), "
        f"{total_services} услуг"
    )
    logger.info("=" * 70)

    export: list[dict] = []

    for cat in cats:
        services = cat.services
        if args.in_stock:
            services = [s for s in services if s.in_stock > 0]
        in_stock_count = sum(1 for s in cat.services if s.in_stock > 0)

        logger.info(
            f"[{cat.category_id}] {cat.category_name}  "
            f"(услуг: {len(cat.services)}, в наличии: {in_stock_count})"
        )
        # Схема полей заказа категории — важна для importer'а (что NS
        # потребует при покупке: player_id, email и т.п.).
        if cat.fields:
            field_keys = ", ".join(
                f"{f.key}{'*' if f.required else ''}" for f in cat.fields
            )
            logger.info(f"      order-fields: {field_keys}")

        cat_export = {
            "category_id": cat.category_id,
            "category_name": cat.category_name,
            "order_fields": [f.key for f in cat.fields],
            "services": [],
        }

        if not args.categories:
            for s in services:
                logger.info(
                    f"      svc_id={s.service_id:<7} "
                    f"{s.service_name[:50]:<50} "
                    f"{s.price:>9.4f} {s.currency:<4} stock={s.in_stock}"
                )
                cat_export["services"].append({
                    "service_id": s.service_id,
                    "service_name": s.service_name,
                    "price": s.price,
                    "currency": s.currency,
                    "in_stock": s.in_stock,
                })

        export.append(cat_export)

    logger.info("=" * 70)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
        logger.success(f"Сохранено в JSON: {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
