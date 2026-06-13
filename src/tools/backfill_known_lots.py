"""
P1-4: разовый backfill KnownLot для маппингов без заголовка.

Матчинг заказов без lot_id использует KnownLot.title (сильный сигнал,
бонус 120). Лоты, созданные миграцией ДО P1-4, могут не иметь KnownLot
(или иметь пустой title). Этот скрипт проставляет title из mapping.label
для всех таких маппингов (оффлайн, без обращений к FunPay; new_lots
discovery потом уточнит title по фактической витрине).

Запуск:
    ./.venv/bin/python -m src.tools.backfill_known_lots          # dry-run
    ./.venv/bin/python -m src.tools.backfill_known_lots --apply  # записать
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger
from sqlalchemy import select

from src.db.models import KnownLot, Mapping
from src.db.session import init_db, session_factory
from src.db.repo import upsert_known_lot
from src.logging_setup import setup_logging


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Backfill KnownLot.title из mapping.label")
    parser.add_argument("--apply", action="store_true", help="записать (иначе dry-run)")
    args = parser.parse_args()

    await init_db()

    async with session_factory()() as session:
        mappings = list((await session.execute(select(Mapping))).scalars().all())
        known = {
            k.funpay_lot_id: k
            for k in (await session.execute(select(KnownLot))).scalars().all()
        }

    to_fill: list[tuple[int, str]] = []
    for m in mappings:
        existing = known.get(m.funpay_lot_id)
        has_title = existing is not None and bool((existing.title or "").strip())
        if has_title:
            continue
        title = (m.label or "").strip()
        if not title:
            logger.warning(f"  lot {m.funpay_lot_id}: нет label — пропуск")
            continue
        to_fill.append((m.funpay_lot_id, title))

    logger.info(
        f"Маппингов: {len(mappings)}, KnownLot: {len(known)}, "
        f"к заполнению title: {len(to_fill)}"
    )
    for lid, title in to_fill[:20]:
        logger.info(f"  lot {lid} ← «{title[:60]}»")
    if len(to_fill) > 20:
        logger.info(f"  … и ещё {len(to_fill) - 20}")

    if not args.apply:
        logger.info("DRY-RUN. Добавь --apply для записи.")
        return 0

    async with session_factory()() as session:
        for lid, title in to_fill:
            await upsert_known_lot(
                session, funpay_lot_id=lid, title=title, mark_notified=True
            )
        await session.commit()
    logger.success(f"Заполнено KnownLot.title для {len(to_fill)} маппингов.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
