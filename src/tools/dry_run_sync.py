"""
CLI: один прогон синхронизатора в режиме dry-run.
Показывает, что бот хотел бы изменить, но НИЧЕГО не пишет на FunPay.

Запуск:
    /opt/funpay-ns-bot/.venv/bin/python -m src.tools.dry_run_sync
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from src.config import get_settings
from src.db.session import close_db, init_db
from src.logging_setup import setup_logging
from src.sync.stock_sync import sync_once


async def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    logger.info("=" * 60)
    logger.info("DRY-RUN SYNC: расчёт без записи на FunPay")
    logger.info("=" * 60)

    await init_db()
    try:
        result = await sync_once(dry_run=True)
    finally:
        await close_db()

    logger.info(
        f"Итог: checked={result['checked']}, "
        f"would_update={result['updated']}, "
        f"skipped={result['skipped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
