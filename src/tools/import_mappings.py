"""
CLI: импорт маппингов NS<->FunPay из CSV в локальную БД.

Запуск (на сервере):
    /opt/funpay-ns-bot/.venv/bin/python -m src.tools.import_mappings \\
        /opt/funpay-ns-bot/data/mappings.csv
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from loguru import logger

from src.config import get_settings
from src.db.session import close_db, init_db
from src.logging_setup import setup_logging
from src.mapping.loader import import_mappings_from_csv


async def main(csv_path: Path) -> int:
    settings = get_settings()
    setup_logging(settings)

    logger.info(f"Импорт маппингов из {csv_path}")
    await init_db()
    try:
        result = await import_mappings_from_csv(csv_path)
    finally:
        await close_db()

    logger.info(
        f"Готово: inserted_or_updated={result['inserted_or_updated']}, "
        f"skipped={result['skipped']}"
    )
    if result["errors"]:
        logger.warning("Были ошибки:")
        for err in result["errors"]:
            logger.warning(f"  {err}")
        return 1
    return 0


def _entry() -> int:
    if len(sys.argv) < 2:
        print("Использование: python -m src.tools.import_mappings <path-to-csv>", file=sys.stderr)
        return 2
    csv_path = Path(sys.argv[1])
    return asyncio.run(main(csv_path))


if __name__ == "__main__":
    sys.exit(_entry())
