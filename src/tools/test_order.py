"""
CLI: прогон order-processor вручную с синтетическими данными FunPay-заказа.

Полезно для отладки, пока нет реального FunPay-watcher'а.

Запуск:
    python -m src.tools.test_order \\
        --funpay-order-id TEST-001 \\
        --funpay-lot-id 12345678 \\
        --quantity 1 \\
        --buyer TestBuyer \\
        [--really]    # без флага = dry-run (NS create_order, но pay_order запрещён)

ВАЖНО: dry-run по умолчанию. С `--really` (и только если ENABLE_REAL_ACTIONS=true
в .env) бот реально создаёт и оплачивает NS-заказ.

Все требования к запуску:
- Маппинг для указанного funpay_lot_id должен быть импортирован в БД
  (через `src.tools.import_mappings`).
- NS API доступен и проверен (`check_ns`).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.alerts.telegram import TelegramNotifier
from src.config import get_settings
from src.db.session import close_db, init_db
from src.logging_setup import setup_logging
from src.orders.processor import FunPayOrderEvent, process_funpay_order


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    setup_logging(settings)
    await init_db()

    event = FunPayOrderEvent(
        funpay_order_id=args.funpay_order_id,
        funpay_lot_id=args.funpay_lot_id,
        buyer_username=args.buyer,
        buyer_user_id=None,
        chat_id=None,  # без FunPay-клиента сообщения в чат не шлём
        quantity=args.quantity,
        funpay_price_rub=args.funpay_price,
    )

    dry_run = not args.really or not settings.enable_real_actions

    if args.really and not settings.enable_real_actions:
        logger.warning(
            "Передан --really, но ENABLE_REAL_ACTIONS=false в .env. "
            "Заказ будет создан, но НЕ оплачен. Включи ENABLE_REAL_ACTIONS=true когда готов."
        )

    try:
        async with TelegramNotifier(settings) as tg:
            result = await process_funpay_order(
                event,
                settings=settings,
                funpay_client=None,  # без FunPay для теста — без чата
                telegram=tg if tg.enabled else None,
                dry_run=dry_run,
            )
    finally:
        await close_db()

    logger.info(f"Результат: {result}")
    return 0 if result.get("status") in {"delivered", "ns_created"} else 1


def _entry() -> int:
    parser = argparse.ArgumentParser(description="Тестовый прогон order processor")
    parser.add_argument("--funpay-order-id", required=True)
    parser.add_argument("--funpay-lot-id", type=int, required=True)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--buyer", default="TestBuyer")
    parser.add_argument("--funpay-price", type=float, default=None)
    parser.add_argument(
        "--really",
        action="store_true",
        help="Реально оплатить (требует ENABLE_REAL_ACTIONS=true)",
    )
    args = parser.parse_args()
    return asyncio.run(main(args))


if __name__ == "__main__":
    sys.exit(_entry())
