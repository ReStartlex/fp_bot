"""
CLI: определить TELEGRAM_CHAT_ID через `/start` боту.

Алгоритм:
1. Опрашиваем getUpdates у Telegram Bot API.
2. Ждём, пока ты не напишешь боту что угодно (например, /start).
3. Берём chat_id из последнего апдейта и печатаем — вставь его в .env.

Запуск:
    python -m src.tools.discover_chat_id
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
from loguru import logger

from src.config import get_settings
from src.logging_setup import setup_logging


POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 25  # long-polling Telegram


async def _get_updates(client: httpx.AsyncClient, token: str, offset: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS}
    if offset is not None:
        params["offset"] = offset
    r = await client.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API: {data}")
    return data.get("result", [])


async def main() -> int:
    settings = get_settings()
    setup_logging(settings)

    if settings.telegram_bot_token is None:
        logger.error("TELEGRAM_BOT_TOKEN не задан в .env")
        return 1

    token = settings.telegram_bot_token.get_secret_value()
    proxy = settings.telegram_proxy_url

    if settings.telegram_chat_id is not None:
        logger.warning(
            f"TELEGRAM_CHAT_ID уже задан в .env ({settings.telegram_chat_id}). "
            "Этот скрипт всё равно покажет твой текущий chat_id для проверки."
        )

    logger.info("=" * 60)
    logger.info("Жду от тебя сообщение боту (/start или что угодно)...")
    logger.info("Открой Telegram → найди своего бота → отправь ему любое сообщение.")
    logger.info("Я опрашиваю Telegram каждые ~25 сек, прерви Ctrl+C если что.")
    logger.info("=" * 60)

    timeout = httpx.Timeout(POLL_TIMEOUT_SECONDS + 5)
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if proxy:
        client_kwargs["proxy"] = proxy

    offset: int | None = None
    seen: set[int] = set()

    async with httpx.AsyncClient(**client_kwargs) as client:
        # Первый запрос: дренируем буфер старых апдейтов, чтобы взять только свежие
        try:
            stale = await _get_updates(client, token, offset=None)
            if stale:
                offset = stale[-1]["update_id"] + 1
                logger.info(f"Сброс {len(stale)} старых апдейтов (offset={offset})")
        except httpx.HTTPError as exc:
            logger.error(f"Не достучался до Telegram: {exc}")
            return 2

        while True:
            try:
                updates = await _get_updates(client, token, offset=offset)
            except httpx.HTTPError as exc:
                logger.warning(f"Telegram getUpdates упал: {exc}. Повтор через 5с.")
                await asyncio.sleep(5)
                continue

            if not updates:
                continue

            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                if chat_id in seen:
                    continue
                seen.add(chat_id)

                from_user = msg.get("from", {})
                username = from_user.get("username")
                first = from_user.get("first_name", "")
                text = msg.get("text", "")
                kind = chat.get("type", "?")

                logger.success("=" * 60)
                logger.success(f"Найдено сообщение:")
                logger.success(f"  chat_id: {chat_id}")
                logger.success(f"  тип:     {kind}")
                logger.success(f"  от:      {first} (@{username or '—'})")
                logger.success(f"  текст:   {text[:80]}")
                logger.success("")
                logger.success("Вставь в .env строку:")
                logger.success(f"  TELEGRAM_CHAT_ID={chat_id}")
                logger.success("=" * 60)
                return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.warning("Прервано пользователем")
        sys.exit(130)
