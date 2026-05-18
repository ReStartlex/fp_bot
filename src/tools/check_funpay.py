"""
Расширенная CLI-диагностика доступа к FunPay.

Что делает:
1. Показывает внешний IP сервера (важно: FunPay может привязывать сессию к IP).
2. HTTP-запрос к funpay.com БЕЗ cookies — что отвечает FunPay вообще.
3. HTTP-запрос к funpay.com С cookies из .env — есть ли в HTML признаки логина.
4. Полноценный login через FunPayAPI и проверка лотов.
5. Доступные атрибуты Account (для разведки API).

Запуск:
    cd /opt/funpay-ns-bot
    ./.venv/bin/python -m src.tools.check_funpay
"""
from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

import httpx
from loguru import logger

from src.config import get_settings
from src.funpay.client import FunPayClient
from src.logging_setup import setup_logging


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def _detect_external_ip() -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with httpx.Client(timeout=5.0) as c:
                r = c.get(url)
                if r.status_code == 200 and r.text.strip():
                    return r.text.strip()
        except Exception:
            continue
    return None


def _http_get(url: str, *, cookies: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        with httpx.Client(
            timeout=15.0,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            follow_redirects=True,
        ) as c:
            r = c.get(url, cookies=cookies or {})
            return {
                "ok": True,
                "status": r.status_code,
                "url_final": str(r.url),
                "elapsed": r.elapsed.total_seconds(),
                "len": len(r.content),
                "text": r.text,
                "headers": dict(r.headers),
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _analyze_funpay_html(html: str) -> dict[str, Any]:
    """
    Ищет в HTML признаки залогиненности.
    FunPay меняет страницу для авторизованных пользователей.
    """
    if not html:
        return {"empty": True}

    text_lower = html.lower()

    # Несколько эвристик, какая сработает — той и поверим
    has_logout = "logout" in text_lower or "выход" in text_lower
    has_login_form = (
        'name="login"' in text_lower
        and ('name="password"' in text_lower or 'type="password"' in text_lower)
    )
    has_user_menu = 'class="user-link-name"' in text_lower or 'class="user-link"' in text_lower
    has_ddos_guard = "ddos-guard" in text_lower or "checking your browser" in text_lower

    username_match = re.search(
        r'class="user-link-name"[^>]*>\s*([^<\n\r]+)\s*<', html, re.IGNORECASE
    )
    username = username_match.group(1).strip() if username_match else None

    # title для контроля
    title_match = re.search(r"<title>([^<]*)</title>", html, re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else None

    return {
        "title": title,
        "has_logout": has_logout,
        "has_login_form": has_login_form,
        "has_user_menu": has_user_menu,
        "has_ddos_guard": has_ddos_guard,
        "username_in_html": username,
    }


def _sanity_check_cookie(name: str, value: str) -> list[str]:
    """Проверяет, не скопировано ли значение с лишними символами."""
    issues: list[str] = []
    if not value:
        issues.append(f"{name}: пустое значение")
        return issues
    if value != value.strip():
        issues.append(f"{name}: есть пробелы в начале/конце")
    if " " in value:
        issues.append(f"{name}: содержит пробел внутри (значит почти точно сломано)")
    if value.startswith('"') or value.endswith('"'):
        issues.append(f"{name}: обёрнуто в двойные кавычки")
    if "%" in value and name == "golden_key":
        issues.append(
            f"{name}: содержит '%' — возможно URL-encoded. "
            "Скопируй сырое значение из cookie, не из адресной строки."
        )
    if name == "golden_key" and not re.fullmatch(r"[a-zA-Z0-9]{20,}", value):
        issues.append(
            f"{name}: формат подозрительный — обычно ~32-40 символов из [a-zA-Z0-9]"
        )
    if name == "PHPSESSID" and not re.fullmatch(r"[a-zA-Z0-9]{20,40}", value):
        issues.append(
            f"{name}: формат подозрительный — обычно 26-32 символа из [a-zA-Z0-9]"
        )
    return issues


async def main() -> int:
    setup_logging()
    settings = get_settings()

    logger.info("=" * 60)
    logger.info("Расширенная диагностика FunPay")
    logger.info("=" * 60)

    logger.info("0. Внешний IP сервера...")
    ip = _detect_external_ip()
    if ip:
        logger.info(f"   IP: {ip}")
    else:
        logger.warning("   Не смог определить внешний IP (не критично)")

    logger.info(
        f"   User ID в .env: {settings.funpay_user_id}, "
        f"язык: {settings.funpay_chat_language}"
    )

    golden = settings.funpay_golden_key.get_secret_value()
    php = (
        settings.funpay_phpsessid.get_secret_value()
        if settings.funpay_phpsessid else ""
    )

    logger.info("1. Sanity-check значений cookies...")
    issues = _sanity_check_cookie("golden_key", golden)
    issues += _sanity_check_cookie("PHPSESSID", php) if php else []
    if issues:
        for x in issues:
            logger.warning(f"   ⚠ {x}")
    else:
        logger.success("   формат cookies выглядит ок")
    logger.info(f"   golden_key: {golden[:6]}...{golden[-4:] if len(golden) > 10 else ''}")
    if php:
        logger.info(f"   PHPSESSID:  {php[:6]}...{php[-4:] if len(php) > 10 else ''}")
    else:
        logger.info("   PHPSESSID:  не задан")

    logger.info("2. GET funpay.com БЕЗ cookies...")
    anon = _http_get("https://funpay.com/")
    if not anon["ok"]:
        logger.error(f"   FunPay недоступен: {anon['error']}")
        return 2
    logger.info(f"   HTTP {anon['status']}, {anon['len']} байт, {anon['elapsed']:.2f}s")
    anon_a = _analyze_funpay_html(anon["text"])
    logger.info(f"   title: {anon_a.get('title')}")
    if anon_a.get("has_ddos_guard"):
        logger.warning("   ⚠ FunPay показывает ddos-guard challenge")

    logger.info("3. GET funpay.com С твоими cookies...")
    cookies = {"golden_key": golden}
    if php:
        cookies["PHPSESSID"] = php
    auth = _http_get("https://funpay.com/", cookies=cookies)
    if not auth["ok"]:
        logger.error(f"   Ошибка: {auth['error']}")
        return 2
    logger.info(f"   HTTP {auth['status']}, {auth['len']} байт")
    a = _analyze_funpay_html(auth["text"])
    logger.info(f"   title: {a.get('title')}")
    logger.info(
        f"   признаки логина: user_menu={a['has_user_menu']}, "
        f"logout={a['has_logout']}, login_form_present={a['has_login_form']}, "
        f"ddos_guard={a['has_ddos_guard']}"
    )
    if a.get("username_in_html"):
        logger.success(f"   ✓ В HTML видно имя пользователя: {a['username_in_html']}")
    if a["has_login_form"] and not a["has_user_menu"]:
        logger.error("   ✗ FunPay возвращает страницу для гостя (login_form, без user_menu).")
        logger.error(
            "     Возможные причины:\n"
            "       а) cookies скопированы неверно (см. предупреждения выше)\n"
            "       б) FunPay привязал сессию к твоему домашнему IP, "
            "          а с IP сервера её не принимает\n"
            "       в) FunPay требует новую верификацию устройства\n"
            "     Что делать: попробуй ещё раз получить cookies через "
            "https://funpay.com → F12 → Application → Cookies. "
            "Если та же ошибка — нужен прокси с домашнего IP."
        )

    logger.info("4. Полный логин через FunPayAPI...")
    try:
        async with FunPayClient() as fp:
            await fp.connect()
            logger.success(
                f"   OK: id={fp.account_id}, username={fp.username}, balance={fp.balance}"
            )
            logger.info("5. Список моих лотов...")
            lots = await fp.get_my_lots()
            logger.success(f"   Лотов: {len(lots)}")
            for lot in lots[:10]:
                lot_id = (
                    getattr(lot, "id", None)
                    or getattr(lot, "lot_id", None)
                    or getattr(lot, "offer_id", None)
                )
                desc_str = (
                    getattr(lot, "description", None)
                    or getattr(lot, "title", None)
                    or repr(lot)
                )
                logger.info(f"     {lot_id}: {str(desc_str)[:80]}")
        logger.success("=" * 60)
        logger.success("FunPay работает.")
        logger.success("=" * 60)
        return 0
    except ImportError as exc:
        logger.error(f"FunPayAPI не установлен: {exc}")
        return 1
    except Exception as exc:
        logger.error(f"FunPayAPI вернул ошибку: {exc}")
        logger.error(
            "Смотри пункт 3 выше — если там login_form_present=True, "
            "это та же проблема: FunPay не считает нашу сессию валидной."
        )
        return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
