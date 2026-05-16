"""
CLI-проверка доступа к ns.gifts.

Запуск:
    python -m src.tools.check_ns

Делает:
0. Печатает внешний (исходящий) IP сервера — он должен быть в whitelist у NS.
1. Логинится (POST /get_token).
2. Запрашивает баланс (GET /check_balance).
3. Запрашивает каталог (GET /stock) и печатает первые 10 услуг.
4. Запрашивает курс USD->RUB для Steam.

Все запросы read-only, никаких покупок не совершается.
"""
from __future__ import annotations

import asyncio

import httpx
from loguru import logger

from src.config import get_settings
from src.logging_setup import setup_logging
from src.ns import NSClient
from src.ns.exceptions import NSAPIError, NSAuthError, NSForbiddenError


def _detect_external_ip() -> str | None:
    """Узнать с какого IP сервер выходит наружу (важно для whitelist)."""
    for url in ("https://api.ipify.org", "https://ifconfig.me", "https://ipv4.icanhazip.com"):
        try:
            with httpx.Client(timeout=5.0, follow_redirects=True) as c:
                r = c.get(url)
                if r.status_code == 200:
                    return r.text.strip()
        except Exception:
            continue
    return None


async def main() -> int:
    setup_logging()
    settings = get_settings()

    logger.info("=" * 60)
    logger.info("Проверка доступа к ns.gifts API v2")
    logger.info(f"  Base URL: {settings.ns_base_url}")
    logger.info(f"  User ID:  {settings.ns_user_id}")
    logger.info(f"  Login:    {settings.ns_login}")
    logger.info(f"  TOTP:     {'есть' if settings.ns_totp_secret else 'нет'}")
    logger.info(f"  Playground режим: {settings.ns_use_playground}")

    logger.info("0/4 Определение внешнего IP сервера...")
    external_ip = _detect_external_ip()
    if external_ip:
        logger.success(f"    Сервер выходит наружу с IP: {external_ip}")
        logger.info(f"    Именно этот IP должен быть в whitelist у ns.gifts.")
    else:
        logger.warning("    Не удалось определить внешний IP (api.ipify.org/ifconfig.me недоступны)")

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
        logger.error(f"Полный ответ сервера:\n{exc.response_body}")
        logger.error("Проверь NS_USER_ID, NS_LOGIN, NS_PASSWORD, NS_API_SECRET в .env")
        return 1
    except NSForbiddenError as exc:
        logger.error(f"403 (forbidden): {exc}")
        logger.error(f"Полный ответ сервера:\n{exc.response_body}")
        if external_ip:
            logger.error(
                f"Внешний IP сервера: {external_ip}. "
                f"Этот IP должен быть в whitelist у ns.gifts."
            )
        logger.error(
            "Если IP в whitelist, но 403 всё равно — возможны причины:\n"
            "  - неверный логин/пароль (некоторые API отдают 403 вместо 401)\n"
            "  - IP добавлен, но изменения ещё не применились\n"
            "  - аккаунт заблокирован/требует верификации\n"
            "Покажи саппорту полный ответ выше — попроси разобраться."
        )
        return 2
    except NSAPIError as exc:
        logger.error(f"NS API ошибка: {exc}")
        logger.error(f"Полный ответ сервера:\n{exc.response_body}")
        return 3
    except Exception as exc:
        logger.exception(f"Неожиданная ошибка: {exc}")
        return 99


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
