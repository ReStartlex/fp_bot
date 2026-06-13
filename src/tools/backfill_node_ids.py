"""
P0-1 Фаза B: разовый backfill Mapping.funpay_node_id для старых лотов.

Snapshot-sync группирует лоты по node (1 GET /lots/{node}/trade на ноду
вместо N offerEdit). Для этого каждому маппингу нужен funpay_node_id.
Новые лоты получают его при создании (migrate/runner); этот скрипт
дозаполняет старые: для каждого маппинга без node делает один
get_lot_fields(lot_id) и берёт оттуда .node_id.

ВАЖНО: throttle между запросами (--delay, default 0.5с), чтобы backfill
сам не устроил 429-шторм. dry-run по умолчанию.

Запуск:
    ./.venv/bin/python -m src.tools.backfill_node_ids            # dry-run
    ./.venv/bin/python -m src.tools.backfill_node_ids --apply
    ./.venv/bin/python -m src.tools.backfill_node_ids --apply --delay 1.0
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.config import get_settings
from src.db.repo import list_mappings_missing_node_id, set_mapping_node_id
from src.db.session import init_db, session_factory
from src.funpay.admin_http import FunPayAdminClient
from src.logging_setup import setup_logging


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
        description="Backfill Mapping.funpay_node_id из get_lot_fields"
    )
    parser.add_argument("--apply", action="store_true", help="записать (иначе dry-run)")
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="пауза между GET-ами лота (сек), защита от 429",
    )
    args = parser.parse_args()

    await init_db()
    settings = get_settings()
    admin = _build_admin(settings)

    async with session_factory()() as session:
        missing = await list_mappings_missing_node_id(session)

    logger.info(f"Маппингов без funpay_node_id: {len(missing)}")
    if not missing:
        logger.success("Всё уже заполнено — backfill не нужен.")
        return 0

    resolved: dict[int, int] = {}
    failed: list[int] = []
    for i, m in enumerate(missing):
        try:
            lot = await admin.get_lot_fields(m.funpay_lot_id)
        except Exception as exc:
            logger.warning(f"  lot {m.funpay_lot_id}: get_lot_fields упал: {exc}")
            failed.append(m.funpay_lot_id)
            await asyncio.sleep(args.delay)
            continue
        node_id = getattr(lot, "node_id", None)
        if node_id is None:
            logger.warning(f"  lot {m.funpay_lot_id}: node_id не распознан — пропуск")
            failed.append(m.funpay_lot_id)
        else:
            resolved[m.funpay_lot_id] = int(node_id)
            logger.info(
                f"  [{i + 1}/{len(missing)}] lot {m.funpay_lot_id} → node {node_id}"
            )
        await asyncio.sleep(args.delay)

    logger.info(f"Распознано node: {len(resolved)}, не удалось: {len(failed)}")

    if not args.apply:
        logger.info("DRY-RUN. Добавь --apply для записи.")
        return 0

    async with session_factory()() as session:
        for lot_id, node_id in resolved.items():
            await set_mapping_node_id(
                session, funpay_lot_id=lot_id, funpay_node_id=node_id
            )
        await session.commit()
    logger.success(f"Записано funpay_node_id для {len(resolved)} маппингов.")
    if failed:
        logger.warning(
            f"Не удалось для {len(failed)} лотов (повтори позже): {failed[:20]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
