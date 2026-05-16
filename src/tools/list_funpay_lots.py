"""
Печать всех твоих лотов на FunPay.

Запуск:
    cd /opt/funpay-ns-bot
    ./.venv/bin/python -m src.tools.list_funpay_lots

Используй после создания лотов в браузере, чтобы узнать lot_id для маппинга.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from loguru import logger

from src.config import get_settings
from src.funpay.client import FunPayClient
from src.logging_setup import setup_logging


def _lot_summary(lot: Any) -> dict[str, Any]:
    """Собрать максимум информации о лоте, не падая на отсутствующих полях."""
    out: dict[str, Any] = {}
    for attr in (
        "id", "lot_id", "title", "description", "description_ru",
        "price", "currency", "amount", "active", "subcategory",
        "subcategory_name", "subcategory_id", "category_id", "server",
        "auto_delivery", "url",
    ):
        if hasattr(lot, attr):
            try:
                val = getattr(lot, attr)
                if callable(val):
                    continue
                out[attr] = val
            except Exception:
                pass
    return out


async def main() -> int:
    setup_logging()
    settings = get_settings()

    logger.info("=" * 60)
    logger.info("Список моих лотов на FunPay")
    logger.info(f"  User ID: {settings.funpay_user_id}")
    logger.info("=" * 60)

    async with FunPayClient() as fp:
        await fp.connect()
        lots = await fp.get_my_lots()

    logger.info(f"Всего лотов: {len(lots)}")
    if not lots:
        logger.warning(
            "Лотов нет. Создай первый лот вручную в браузере на funpay.com — "
            "например для пилота Apple Gift Card USA 2 USD."
        )
        return 0

    # Группируем по subcategory_id чтобы было удобно мапить
    by_subcat: dict[Any, list[Any]] = {}
    for lot in lots:
        summary = _lot_summary(lot)
        key = summary.get("subcategory") or summary.get("subcategory_name") or "?"
        by_subcat.setdefault(key, []).append(summary)

    for subcat, items in by_subcat.items():
        logger.info(f"--- {subcat} ({len(items)} лотов) ---")
        for s in items:
            lot_id = s.get("id") or s.get("lot_id") or "?"
            title = (
                s.get("description_ru")
                or s.get("description")
                or s.get("title")
                or "?"
            )
            price = s.get("price")
            currency = s.get("currency", "")
            amount = s.get("amount", "?")
            active = s.get("active")
            url = s.get("url", "")
            logger.info(
                f"  lot_id={lot_id}  active={active}  amount={amount}  "
                f"price={price}{currency}  «{str(title)[:60]}»"
            )
            if url:
                logger.info(f"           url: {url}")

    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
