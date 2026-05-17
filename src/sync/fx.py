"""Получение курса USD->RUB."""
from __future__ import annotations

import httpx
from loguru import logger

from src.config import RateMode, Settings, get_settings
from src.db.repo import latest_fx_rate, save_fx_rate
from src.db.session import session_factory


CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_TTL_SECONDS = 600  # 10 минут


_cache: tuple[float, float] | None = None  # (timestamp_when_fetched, rate)


async def _fetch_cbr_rate() -> float:
    """USD/RUB с cbr-xml-daily.ru."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(CBR_URL)
        r.raise_for_status()
        data = r.json()
        return float(data["Valute"]["USD"]["Value"])


async def get_usd_rub_rate(settings: Settings | None = None) -> float:
    """
    Получить курс USD->RUB.

    В режиме manual возвращает USD_RUB_RATE.
    В режиме auto пытается обновить с cbr-xml-daily, при ошибке возвращает кэш / fallback.
    """
    settings = settings or get_settings()

    if settings.usd_rub_rate_mode == RateMode.MANUAL:
        return settings.usd_rub_rate

    global _cache
    import time
    now = time.time()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    try:
        rate = await _fetch_cbr_rate()
        _cache = (now, rate)
        logger.info(f"USD/RUB обновлён с ЦБ РФ: {rate:.4f}")
        # Параллельно пишем в БД (best-effort)
        try:
            async with session_factory()() as session:
                await save_fx_rate(session, pair="USD/RUB", rate=rate, source="cbr-xml-daily")
                await session.commit()
        except Exception as exc:
            logger.debug(f"Не записал FX в БД (не критично): {exc}")
        return rate
    except Exception as exc:
        logger.warning(f"Не удалось получить курс USD/RUB с ЦБ: {exc}. Беру кэш/fallback.")

    # Пробуем кэш в памяти
    if _cache is not None:
        return _cache[1]

    # Пробуем кэш в БД
    try:
        async with session_factory()() as session:
            db_rate = await latest_fx_rate(session, "USD/RUB")
            if db_rate is not None:
                logger.info(f"Беру кэш из БД: USD/RUB={db_rate.rate:.4f}")
                return db_rate.rate
    except Exception as exc:
        logger.debug(f"БД-кэш недоступен: {exc}")

    # Fallback из настроек
    logger.warning(f"Использую fallback курс из .env: USD/RUB={settings.usd_rub_rate}")
    return settings.usd_rub_rate
