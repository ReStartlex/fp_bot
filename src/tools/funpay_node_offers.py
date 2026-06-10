"""
Список МОИХ офферов в разделе FunPay, включая неактивные (read-only).

Зачем: get_my_lots() берёт лоты с публичного профиля, где неактивные
офферы не видны. Для importer'а NS→FunPay после создания лота нужно
надёжно узнать его lot_id — этот инструмент парсит страницу управления
офферами раздела `/lots/{node}/trade`, где видны все свои офферы.

Запуск:
    ./.venv/bin/python -m src.tools.funpay_node_offers 1316
    ./.venv/bin/python -m src.tools.funpay_node_offers 1316 --grep "2 USD"
    ./.venv/bin/python -m src.tools.funpay_node_offers 1316 --raw-out /root/trade_1316.html

--grep STR    — показать только офферы, в заголовке которых есть STR.
--raw-out P   — сохранить сырой HTML страницы (для отладки парсера, если
                офферы не распознались).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.config import get_settings
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
        description="Список моих офферов раздела FunPay (вкл. неактивные)"
    )
    parser.add_argument("node_id", type=int, help="node_id раздела")
    parser.add_argument("--grep", type=str, default=None, help="фильтр по заголовку")
    parser.add_argument(
        "--raw-out", type=str, default=None,
        help="сохранить сырой HTML (для отладки парсера)",
    )
    args = parser.parse_args()

    settings = get_settings()
    admin = _build_admin(settings)

    logger.info("=" * 60)
    logger.info(f"Мои офферы в разделе node={args.node_id}")
    logger.info("=" * 60)

    # Сырой HTML по запросу — до парсинга, чтобы отладить даже при пустом
    # результате.
    if args.raw_out:
        url = f"{admin.BASE}/lots/{args.node_id}/trade"
        r = await asyncio.to_thread(admin._sync_get, url)
        with open(args.raw_out, "w", encoding="utf-8") as f:
            f.write(r.text)
        logger.info(f"Сырой HTML сохранён: {args.raw_out} ({len(r.text)} байт)")

    offers = await admin.list_node_offers(args.node_id)

    if args.grep:
        needle = args.grep.lower()
        offers = [o for o in offers if needle in (o["title"] or "").lower()]

    logger.info(f"Найдено офферов: {len(offers)}")
    if not offers:
        logger.warning(
            "Офферы не распознаны. Запусти с --raw-out /root/trade_dump.html "
            "и пришли файл — доработаю парсер под реальную вёрстку."
        )
        return 0

    for o in offers:
        active = o["active"]
        active_str = (
            "🟢 active" if active is True
            else "⚪ inactive" if active is False
            else "? unknown"
        )
        logger.info(
            f"  offer_id={o['offer_id']}  {active_str}  «{o['title'][:70]}»"
        )

    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
