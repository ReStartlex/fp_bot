"""
Отчёт о пригодности категорий NS + генерация YAML-скелета миграции.

Read-only: ничего не создаёт на FunPay, только анализирует каталог NS.

Запуск:
    # сводка по всему каталогу: сколько пригодно/непригодно и почему
    ./.venv/bin/python -m src.tools.migrate_skeleton --report

    # скелет YAML по категориям с «apple» в названии (только в наличии)
    ./.venv/bin/python -m src.tools.migrate_skeleton --grep apple --out /root/migrate_apple.yaml

    # скелет по конкретным категориям
    ./.venv/bin/python -m src.tools.migrate_skeleton --cat-id 4 --cat-id 5 --out /root/m.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.logging_setup import setup_logging
from src.migrate.catalog import build_skeleton_entry, classify_category
from src.migrate.skeleton_yaml import render_skeleton
from src.ns import NSClient


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Пригодность категорий NS + YAML-скелет миграции"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="сводка пригодности по всему каталогу (без генерации YAML)",
    )
    parser.add_argument("--grep", type=str, default=None, help="фильтр категорий по имени")
    parser.add_argument(
        "--cat-id", type=int, action="append", default=[],
        help="конкретная категория (можно повторять)",
    )
    parser.add_argument(
        "--include-ineligible", action="store_true",
        help="включать непригодные категории в скелет (по умолчанию нет)",
    )
    parser.add_argument("--out", type=str, default=None, help="путь для YAML-скелета")
    args = parser.parse_args()

    async with NSClient() as ns:
        await ns.login()
        stock = await ns.get_stock()

    cats = stock.categories
    if args.grep:
        needle = args.grep.lower()
        cats = [c for c in cats if needle in (c.category_name or "").lower()]
    if args.cat_id:
        wanted = set(args.cat_id)
        cats = [c for c in cats if c.category_id in wanted]

    # --- Режим отчёта ---
    if args.report or not args.out:
        eligibilities = [classify_category(c) for c in stock.categories]
        eligible = [e for e in eligibilities if e.eligible]
        # Группируем непригодные по причине (укрупнённо).
        from collections import Counter
        reasons = Counter()
        for e in eligibilities:
            if not e.eligible:
                key = e.reason.split(":")[0].split("(")[0].strip()
                reasons[key] += 1

        logger.info("=" * 70)
        logger.info(
            f"NS каталог: {len(eligibilities)} категорий. "
            f"ПРИГОДНО к миграции: {len(eligible)}"
        )
        logger.info("=" * 70)
        logger.info("Причины непригодности (укрупнённо):")
        for reason, count in reasons.most_common():
            logger.info(f"  {count:>4}  {reason}")
        logger.info("-" * 70)

        scope = cats if (args.grep or args.cat_id) else stock.categories
        scope_elig = [classify_category(c) for c in scope]
        if args.grep or args.cat_id:
            logger.info(f"Категории в фильтре ({len(scope_elig)}):")
            for e in sorted(scope_elig, key=lambda x: (not x.eligible, x.category_name)):
                mark = "✅" if e.eligible else "⛔"
                logger.info(
                    f"  {mark} [{e.category_id}] {e.category_name}  "
                    f"({e.in_stock_services}/{e.total_services} в наличии)  — {e.reason}"
                )
        else:
            logger.info("Пригодные категории (топ по наличию):")
            for e in sorted(eligible, key=lambda x: -x.in_stock_services)[:40]:
                logger.info(
                    f"  ✅ [{e.category_id}] {e.category_name}  "
                    f"({e.in_stock_services} в наличии)"
                )
            logger.info(f"  … всего пригодных: {len(eligible)}")
        logger.info("=" * 70)

        if not args.out:
            logger.info(
                "Для генерации YAML-скелета добавь --out <файл> "
                "(и при желании --grep/--cat-id)."
            )
            return 0

    # --- Режим генерации скелета ---
    selected = []
    for c in cats:
        elig = classify_category(c)
        if elig.eligible or args.include_ineligible:
            selected.append(c)

    if not selected:
        logger.warning(
            "Нет пригодных категорий в выборке. Уточни --grep/--cat-id "
            "или добавь --include-ineligible."
        )
        return 0

    entries = [build_skeleton_entry(c) for c in selected]
    yaml_text = render_skeleton(entries)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(yaml_text)
    logger.success(
        f"Скелет на {len(entries)} категорий сохранён: {args.out}. "
        f"Заполни TODO-поля по схеме FunPay-разделов."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
