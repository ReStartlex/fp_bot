"""
CLI-проверка доступа к ns.gifts.

Запуск:
    python -m src.tools.check_ns

Делает:
1. Логинится (POST /get_token).
2. Запрашивает баланс (GET /check_balance).
3. Запрашивает каталог (GET /stock) и печатает первые 10 услуг.
4. Запрашивает курс USD->RUB для Steam.

Все запросы read-only, никаких покупок не совершается.
"""
from __future__ import annotations

import asyncio

from loguru import logger

from src.config import get_settings
from src.logging_setup import setup_logging
from src.ns import NSClient
from src.ns.exceptions import NSAPIError, NSAuthError, NSForbiddenError


async def main() -> int:
    setup_logging()
    settings = get_settings()

    logger.info("=" * 60)
    logger.info("Проверка доступа к ns.gifts API v2")
    logger.info(f"  Base URL: {settings.ns_base_url}")
    logger.info(f"  User ID:  {settings.ns_user_id}")
    logger.info(f"  Login:    {settings.ns_login}")
    logger.info(f"  Playground режим: {settings.ns_use_playground}")
    logger.info("=" * 60)

    try:
        async with NSClient() as ns:
            logger.info("1/4 Логин...")
            await ns.login()
            logger.success("    OK, токен получен")

            logger.info("2/4 Запрос баланса...")
            balance = await ns.check_balance()
            logger.success(f"    Баланс: {balance.balance} USD")
            if float(balance.balance) < settings.ns_low_balance_threshold:
                logger.warning(
                    f"    !!! Баланс ниже порога "
                    f"{settings.ns_low_balance_threshold} USD"
                )

            logger.info("3/4 Запрос каталога...")
            stock = await ns.get_stock()
            total_services = sum(len(c.services) for c in stock.categories)
            logger.success(
                f"    OK: {len(stock.categories)} категорий, "
                f"{total_services} услуг"
            )
            shown = 0
            for cat in stock.categories:
                if shown >= 10:
                    break
                logger.info(f"    [{cat.category_id}] {cat.category_name}")
                for svc in cat.services[:3]:
                    if shown >= 10:
                        break
                    logger.info(
                        f"       svc_id={svc.service_id:<6} "
                        f"{svc.service_name[:40]:<40} "
                        f"{svc.price:>8.4f} {svc.currency}  "
                        f"stock={svc.in_stock}"
                    )
                    shown += 1

            logger.info("4/4 Запрос курса USD->RUB (Steam)...")
            try:
                rate = await ns.get_exchange_rate(service_id=1)
                logger.success(
                    f"    Курсы NS: RUB={rate.rates.rub}, "
                    f"KZT={rate.rates.kzt}, UAH={rate.rates.uah}"
                )
            except NSAPIError as exc:
                logger.warning(f"    Курс не получили: {exc}")

        logger.success("=" * 60)
        logger.success("Все проверки пройдены. NS API работает.")
        logger.success("=" * 60)
        return 0

    except NSAuthError as exc:
        logger.error(f"401 (auth): {exc}")
        logger.error("Проверь NS_USER_ID, NS_LOGIN, NS_PASSWORD, NS_API_SECRET в .env")
        return 1
    except NSForbiddenError as exc:
        logger.error(f"403 (forbidden): {exc}")
        logger.error(
            "Твой IP не в whitelist у ns.gifts. "
            "Напиши в саппорт и попроси добавить IP сервера. "
            "Пока можно работать через playground (NS_USE_PLAYGROUND=true), "
            "но он не позволяет реально оплачивать заказы."
        )
        return 2
    except NSAPIError as exc:
        logger.error(f"NS API ошибка: {exc}")
        return 3
    except Exception as exc:
        logger.exception(f"Неожиданная ошибка: {exc}")
        return 99


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
