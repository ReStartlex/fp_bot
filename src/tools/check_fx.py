"""
CLI: проверка получения курса USD->RUB.

Запуск:
    /opt/funpay-ns-bot/.venv/bin/python -m src.tools.check_fx
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from src.config import get_settings
from src.db.session import close_db, init_db
from src.logging_setup import setup_logging
from src.sync.fx import get_usd_rub_rate


async def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    await init_db()
    try:
        logger.info(f"Режим: {settings.usd_rub_rate_mode.value}")
        logger.info(f"Fallback из .env: {settings.usd_rub_rate}")
        rate = await get_usd_rub_rate(settings)
        logger.success(f"Итоговый курс USD/RUB = {rate:.4f}")
    finally:
        await close_db()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
