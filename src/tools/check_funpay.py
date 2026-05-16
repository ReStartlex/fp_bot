"""
CLI-проверка доступа к FunPay.

Запуск:
    cd /opt/funpay-ns-bot
    ./.venv/bin/python -m src.tools.check_funpay

Делает:
1. Проверяет HTTP-доступ к funpay.com с сервера.
2. Логинится через golden_key + PHPSESSID.
3. Печатает username, account_id, баланс.
4. Печатает доступные публичные атрибуты Account (для разведки API).
5. Пробует прочитать список твоих лотов.

Никаких записей/изменений не производит.
"""
from __future__ import annotations

import asyncio
import sys

import httpx
from loguru import logger

from src.config import get_settings
from src.funpay.client import FunPayClient
from src.logging_setup import setup_logging


def _http_ping(url: str, timeout: float = 8.0) -> tuple[int, float] | tuple[None, str]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as c:
            r = c.get(url)
            return r.status_code, r.elapsed.total_seconds()
    except Exception as exc:
        return None, str(exc)


async def main() -> int:
    setup_logging()
    settings = get_settings()

    logger.info("=" * 60)
    logger.info("Проверка доступа к FunPay")
    logger.info(f"  User ID:  {settings.funpay_user_id}")
    logger.info(f"  Язык чата: {settings.funpay_chat_language}")
    logger.info("=" * 60)

    logger.info("1/4 HTTP-доступность funpay.com...")
    status, info = _http_ping("https://funpay.com/")
    if status is None:
        logger.error(f"    funpay.com недоступен с сервера: {info}")
        logger.error(
            "    Если ты на Timeweb и Timeweb режет TCP к funpay.com — "
            "потребуется прокси (как с github.com). Скажи мне."
        )
        return 2
    logger.success(f"    funpay.com → HTTP {status}, время {info:.2f}s")

    logger.info("2/4 Логин по golden_key...")
    try:
        async with FunPayClient() as fp:
            await fp.connect()
            logger.success(
                f"    OK: account_id={fp.account_id}, "
                f"username={fp.username}, balance={fp.balance}"
            )

            logger.info("3/4 Разведка API установленной версии FunPayAPI...")
            desc = fp.describe_account()
            attrs_normal = {k: v for k, v in desc.items() if not v.startswith("<error")}
            methods = {k: v for k, v in attrs_normal.items() if v.startswith("<method>")}
            fields = {k: v for k, v in attrs_normal.items() if not v.startswith("<method>")}

            logger.info(f"    Полей: {len(fields)}, методов: {len(methods)}")
            logger.info("    Доступные поля:")
            for name, type_name in sorted(fields.items()):
                logger.info(f"      {name}: {type_name}")
            logger.info("    Доступные методы:")
            for name, sig in sorted(methods.items()):
                logger.info(f"      {name}{sig[len('<method>'):]}")

            logger.info("4/4 Попытка получить мои лоты...")
            lots = await fp.get_my_lots()
            logger.success(f"    Получено лотов: {len(lots)}")
            for lot in lots[:5]:
                desc_str = getattr(lot, "description", None) or getattr(
                    lot, "title", repr(lot)
                )
                logger.info(f"      - {desc_str}")
            if len(lots) > 5:
                logger.info(f"      ... и ещё {len(lots) - 5} лотов")

        logger.success("=" * 60)
        logger.success("FunPay работает.")
        logger.success("=" * 60)
        return 0

    except ImportError as exc:
        logger.error(f"FunPayAPI не установлен: {exc}")
        logger.error("Установка: pip install FunPayAPI")
        return 1
    except Exception as exc:
        logger.exception(f"Ошибка при работе с FunPay: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
