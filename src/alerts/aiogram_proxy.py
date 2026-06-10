"""
Сборка aiogram-сессии с поддержкой прокси для long-polling ботов.

Проблема, которую это решает: на РФ-VPS прямой доступ к api.telegram.org
регулярно блокируется. Уведомления (`alerts/telegram.py`) уже ходят через
SOCKS5-прокси своим httpx-клиентом, а вот aiogram-боты (admin `alerts/bot.py`
и `shop/bot.py`) создавались как `Bot(token=...)` без session — и их
long-polling шёл напрямую, утыкаясь в `TelegramNetworkError: Request timeout`.
Меню, кнопки, обработка команд при этом не работали.

Здесь один хелпер строит `AiohttpSession(proxy=...)` из тех же
`TELEGRAM_PROXY_*`, что использует нотификатор. Для SOCKS5 aiogram
требует пакет `aiohttp_socks` (см. requirements.txt).
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from src.config import Settings


def build_aiogram_session(settings: Settings, *, bot_label: str) -> Any | None:
    """
    Возвращает `AiohttpSession` с прокси, либо None (прямое соединение).

    None означает «aiogram создаст дефолтную direct-сессию сам» — это
    штатно для машин, где Telegram доступен напрямую.

    Намеренно НЕ делаем авто-fallback на direct при ошибке прокси:
    на РФ-VPS direct не работает в принципе, и тихий fallback просто
    спрятал бы проблему. Если прокси задан, но мёртв — пусть лог честно
    показывает сетевые ошибки против прокси.
    """
    proxy_url = settings.telegram_proxy_url
    if not proxy_url:
        logger.info(
            f"Telegram {bot_label}: long-polling БЕЗ прокси (direct). "
            f"Если бот на РФ-сервере не отвечает — задай TELEGRAM_USE_PROXY=true "
            f"и TELEGRAM_PROXY_*."
        )
        return None

    try:
        from aiogram.client.session.aiohttp import AiohttpSession
    except Exception as exc:  # pragma: no cover — aiogram всегда установлен
        logger.error(
            f"Telegram {bot_label}: не смог импортировать AiohttpSession ({exc}); "
            f"бот пойдёт напрямую и, скорее всего, не достучится."
        )
        return None

    scheme = settings.telegram_proxy_type.value
    if scheme.startswith("socks"):
        # aiogram использует aiohttp_socks для SOCKS-прокси. Если пакета
        # нет — даём понятную ошибку вместо невнятного ImportError при
        # первом запросе.
        try:
            import aiohttp_socks  # noqa: F401
        except Exception:
            logger.error(
                f"Telegram {bot_label}: для SOCKS5-прокси нужен пакет "
                f"aiohttp_socks, а он не установлен. Выполни "
                f"`pip install aiohttp_socks` (он есть в requirements.txt — "
                f"перезапусти deploy/update.sh). Пока бот пойдёт напрямую."
            )
            return None

    try:
        session = AiohttpSession(proxy=proxy_url)
    except Exception as exc:
        logger.error(
            f"Telegram {bot_label}: не смог создать прокси-сессию "
            f"({type(exc).__name__}: {exc}); иду напрямую."
        )
        return None

    logger.info(
        f"Telegram {bot_label}: long-polling ЧЕРЕЗ прокси "
        f"{settings.telegram_proxy_host}:{settings.telegram_proxy_port} ({scheme})"
    )
    return session
