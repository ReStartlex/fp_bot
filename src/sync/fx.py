"""
Получение курса USD->RUB.

Архитектура курса:

    биржевой курс (ЦБ или ручной)  ──premium──>  эффективный курс
                                                       │
                                                       ▼
                                          compute_pricing (markup)

В режиме AUTO базовый курс берём с cbr-xml-daily.ru и кэшируем, к нему
добавляем premium (% поверх) — это компенсация разницы между биржевым
и реальным курсом покупки USD (на Bybit, P2P и т.п.).

В режиме MANUAL premium игнорируется: предполагается, что в `USD_RUB_RATE`
уже зашит финальный курс, который хочет видеть оператор.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from loguru import logger

from src.config import RateMode, Settings, get_settings
from src.db.repo import latest_fx_rate, save_fx_rate
from src.db.session import session_factory


CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_TTL_SECONDS = 600  # 10 минут


_cache: tuple[float, float] | None = None  # (timestamp_when_fetched, rate)


@dataclass(frozen=True)
class RateBreakdown:
    """Детализированный курс — для UI и логов."""
    base: float
    premium_percent: float
    effective: float
    source: str  # "cbr" / "manual" / "cache_mem" / "cache_db" / "fallback"

    @property
    def has_premium(self) -> bool:
        return self.premium_percent > 0


async def _fetch_cbr_rate() -> float:
    """USD/RUB с cbr-xml-daily.ru."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(CBR_URL)
        r.raise_for_status()
        data = r.json()
        return float(data["Valute"]["USD"]["Value"])


def _apply_premium(base: float, mode: RateMode, premium_percent: float) -> float:
    """База + premium → эффективный курс. В MANUAL premium игнорируется."""
    if mode == RateMode.MANUAL:
        return base
    return base * (1.0 + premium_percent / 100.0)


async def get_rate_breakdown(settings: Settings | None = None) -> RateBreakdown:
    """
    Получить детализированный курс: базовый + premium + эффективный.
    Премия читается с учётом runtime-оверрайда из БД.
    """
    settings = settings or get_settings()
    # Лениво подтягиваем premium из runtime-override; fallback на .env
    from src.config_runtime import get_premium_percent

    premium = (
        await get_premium_percent(settings)
        if settings.usd_rub_rate_mode == RateMode.AUTO
        else 0.0
    )

    if settings.usd_rub_rate_mode == RateMode.MANUAL:
        return RateBreakdown(
            base=settings.usd_rub_rate,
            premium_percent=0.0,
            effective=settings.usd_rub_rate,
            source="manual",
        )

    global _cache
    import time
    now = time.time()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        base = _cache[1]
        return RateBreakdown(
            base=base, premium_percent=premium,
            effective=_apply_premium(base, settings.usd_rub_rate_mode, premium),
            source="cache_mem",
        )

    try:
        base = await _fetch_cbr_rate()
        _cache = (now, base)
        effective = _apply_premium(base, settings.usd_rub_rate_mode, premium)
        logger.info(
            f"USD/RUB ЦБ={base:.4f}, +{premium:.1f}% = "
            f"{effective:.4f} (эффективный)"
        )
        try:
            async with session_factory()() as session:
                await save_fx_rate(
                    session, pair="USD/RUB", rate=base, source="cbr-xml-daily"
                )
                await session.commit()
        except Exception as exc:
            logger.debug(f"Не записал FX в БД (не критично): {exc}")
        return RateBreakdown(
            base=base, premium_percent=premium, effective=effective, source="cbr"
        )
    except Exception as exc:
        logger.warning(f"Не удалось получить курс USD/RUB с ЦБ: {exc}. Беру кэш/fallback.")

    if _cache is not None:
        base = _cache[1]
        return RateBreakdown(
            base=base, premium_percent=premium,
            effective=_apply_premium(base, settings.usd_rub_rate_mode, premium),
            source="cache_mem",
        )

    try:
        async with session_factory()() as session:
            db_rate = await latest_fx_rate(session, "USD/RUB")
            if db_rate is not None:
                logger.info(f"Беру кэш из БД: USD/RUB={db_rate.rate:.4f}")
                return RateBreakdown(
                    base=db_rate.rate, premium_percent=premium,
                    effective=_apply_premium(
                        db_rate.rate, settings.usd_rub_rate_mode, premium
                    ),
                    source="cache_db",
                )
    except Exception as exc:
        logger.debug(f"БД-кэш недоступен: {exc}")

    logger.warning(f"Использую fallback курс из .env: USD/RUB={settings.usd_rub_rate}")
    return RateBreakdown(
        base=settings.usd_rub_rate, premium_percent=premium,
        effective=_apply_premium(
            settings.usd_rub_rate, settings.usd_rub_rate_mode, premium
        ),
        source="fallback",
    )


async def get_usd_rub_rate(settings: Settings | None = None) -> float:
    """Эффективный курс USD->RUB с уже применённым premium. Это то, что
    используется в compute_pricing и определяет цену лота."""
    breakdown = await get_rate_breakdown(settings)
    return breakdown.effective
