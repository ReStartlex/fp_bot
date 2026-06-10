"""
Валидация записи таблицы соответствий против реальной схемы FunPay-раздела.

READ-ONLY: ничего не создаёт на FunPay. Сверяет каждое select-значение
(валюта, номинал...) с опциями раздела и показывает, какие услуги
создадутся, а какие нет (и почему). Это «идеально без ошибок» —
несоответствия видно ДО любого создания лотов.

Запуск:
    ./.venv/bin/python -m src.tools.migrate_validate /root/migrate_apple.yaml --category 4
    ./.venv/bin/python -m src.tools.migrate_validate /root/migrate_apple.yaml --all
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.config import get_settings
from src.funpay.admin_http import FunPayAdminClient
from src.logging_setup import setup_logging
from src.migrate.generator import build_creation_fields, validate_entry_against_schema
from src.migrate.loader import MigrationEntry, load_entries
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


async def _validate_one(
    entry: MigrationEntry, admin: FunPayAdminClient, fx_rate: float, *, show_preview: bool
) -> bool:
    logger.info("=" * 70)
    logger.info(f"[{entry.ns_category_id}] {entry.ns_category_name} → node {entry.funpay_node}")

    problems = entry.todo_problems()
    if problems:
        logger.error("Запись не готова — заполни:")
        for p in problems:
            logger.error(f"  • {p}")
        return False

    # Диагностика пустого блока services (частая жертва ручной правки YAML).
    if not entry.services:
        logger.error(
            "В записи НЕТ services — список услуг пуст. Скорее всего, блок "
            "services: повреждён правкой. Перегенерируй файл "
            "(migrate_skeleton --grep ... --out ...) и используй CLI-оверрайды "
            "(--funpay-node / --set-field) вместо ручной правки YAML."
        )
        return False
    if not entry.in_stock_services():
        logger.warning(
            f"Все {len(entry.services)} услуг категории сейчас не в наличии "
            f"(in_stock=0) — создавать нечего."
        )
        return False

    # Схема раздела (live, read-only GET).
    url = f"{admin.BASE}/lots/offerEdit?node={entry.funpay_node}"
    r = await asyncio.to_thread(admin._sync_get, url)
    schema = parse_form_schema(r.text, url)

    result = validate_entry_against_schema(entry, schema, fx_rate)

    for w in result.field_name_warnings:
        logger.warning(f"  ⚠ {w}")

    ok = result.ok_services
    bad = result.bad_services
    logger.info(
        f"Услуг в наличии: {len(result.services)}  →  "
        f"создадутся: {len(ok)}, пропустятся: {len(bad)}"
    )

    for sv in ok:
        n = sv.service.nominal
        logger.info(
            f"  ✅ svc {sv.service.service_id}  номинал {n}  "
            f"цена ~{sv.price_rub}₽  stock={sv.service.in_stock}"
        )
    for sv in bad:
        logger.warning(
            f"  ⛔ svc {sv.service.service_id} номинал {sv.service.nominal}: "
            + "; ".join(sv.reasons)
        )

    if show_preview and ok:
        sample = ok[0]
        fields = build_creation_fields(entry, sample.service, fx_rate)
        logger.info("-" * 70)
        logger.info(f"Превью лота (svc {sample.service.service_id}):")
        logger.info(f"  summary[ru]: {fields.get('fields[summary][ru]')}")
        logger.info(f"  summary[en]: {fields.get('fields[summary][en]')}")
        logger.info(f"  цена: ~{sample.price_rub}₽, amount будет выставлен sync'ом")
        sel_preview = {
            k: v for k, v in fields.items()
            if not k.startswith("fields[summary]") and not k.startswith("fields[desc]")
        }
        logger.info(f"  поля-селекты: {sel_preview}")

    if bad:
        logger.warning(
            "Часть услуг не пройдёт — для них в разделе нет нужного "
            "значения (валюта/номинал). Они будут пропущены при создании."
        )
    return len(ok) > 0


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Валидация записи миграции против схемы FunPay-раздела"
    )
    parser.add_argument("yaml_path", help="путь к YAML-таблице соответствий")
    parser.add_argument("--category", type=int, default=None, help="ns_category_id")
    parser.add_argument("--all", action="store_true", help="проверить все записи файла")
    parser.add_argument(
        "--no-preview", action="store_true", help="не показывать превью лота",
    )
    # CLI-оверрайды: позволяют проверить категорию БЕЗ правки YAML.
    # Удобно на этапе подбора node/полей — не трогаем хрупкий YAML вручную.
    parser.add_argument(
        "--funpay-node", type=int, default=None,
        help="переопределить funpay_node (без правки YAML)",
    )
    parser.add_argument(
        "--set-field", action="append", default=[], metavar="NAME=VALUE",
        help="задать поле funpay_fields (повторяемый); заменяет funpay_fields целиком",
    )
    parser.add_argument(
        "--markup", type=float, default=None, help="переопределить markup_percent",
    )
    args = parser.parse_args()

    entries = load_entries(args.yaml_path)
    if not args.all:
        if args.category is None:
            logger.error("Укажи --category <ns_category_id> или --all")
            return 1
        entries = [e for e in entries if e.ns_category_id == args.category]
        if not entries:
            logger.error(f"Категория {args.category} не найдена в {args.yaml_path}")
            return 1

    # Применяем CLI-оверрайды (только при единичной категории — иначе
    # неоднозначно, к какой записи их относить).
    overrides_fields: dict[str, str] = {}
    for raw in args.set_field:
        if "=" not in raw:
            logger.error(f"--set-field ожидает NAME=VALUE, получил: {raw!r}")
            return 1
        name, _, value = raw.partition("=")
        overrides_fields[name.strip()] = value
    if args.funpay_node is not None or overrides_fields or args.markup is not None:
        if len(entries) != 1:
            logger.error(
                "CLI-оверрайды (--funpay-node/--set-field/--markup) работают "
                "только с одной категорией (--category)."
            )
            return 1
        e = entries[0]
        if args.funpay_node is not None:
            e.funpay_node = args.funpay_node
        if overrides_fields:
            e.funpay_fields = overrides_fields
        if args.markup is not None:
            e.markup_percent = args.markup
        logger.info(
            f"Применены оверрайды: node={e.funpay_node}, "
            f"fields={e.funpay_fields}, markup={e.markup_percent}"
        )

    settings = get_settings()
    fx_rate = await get_usd_rub_rate(settings)
    logger.info(f"Курс USD→RUB: {fx_rate:.4f}, markup из записи применится сверху")
    admin = _build_admin(settings)

    any_ok = False
    for entry in entries:
        try:
            ok = await _validate_one(
                entry, admin, fx_rate, show_preview=not args.no_preview
            )
            any_ok = any_ok or ok
        except Exception as exc:
            logger.error(f"[{entry.ns_category_id}] валидация упала: {exc}")

    logger.info("=" * 70)
    return 0 if any_ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
