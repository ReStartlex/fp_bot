"""
CLI: проверка отправки сообщения в Telegram.

Запуск:
    python -m src.tools.check_telegram
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from loguru import logger

from src.alerts.telegram import TelegramNotifier
from src.config import get_settings
from src.logging_setup import setup_logging


async def main() -> int:
    settings = get_settings()
    setup_logging(settings)

    if not settings.telegram_enabled:
        logger.warning("TELEGRAM_ENABLED=false в .env — Telegram отключён.")
        return 0
    if settings.telegram_bot_token is None:
        logger.error("TELEGRAM_BOT_TOKEN не задан в .env")
        return 1
    if settings.telegram_chat_id is None:
        logger.error("TELEGRAM_CHAT_ID не задан в .env")
        return 1

    proxy = settings.telegram_proxy_url
    logger.info(f"Прокси: {'есть' if proxy else 'нет (прямой)'}")
    logger.info(f"Chat ID: {settings.telegram_chat_id}")

    async with TelegramNotifier(settings) as tg:
        ok = await tg.info(
            f"Тест Telegram-нотификатора от ns-funpay-bridge\n"
            f"Время: <code>{datetime.now().isoformat(timespec='seconds')}</code>"
        )
    if ok:
        logger.success("Сообщение отправлено. Проверь в Telegram.")
        return 0
    logger.error("Не удалось отправить сообщение. Смотри лог выше.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
