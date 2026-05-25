"""
Фоновый воркер: подтягивает каталог из NS.gifts и обновляет
shop_catalog_cache. Запускается из APScheduler каждые
SHOP_CATALOG_REFRESH_SECONDS (default 90с).

Решения:
- Идемпотентно: повторный запуск без изменений в NS — одинаковый результат.
- Защита от network blip: NS вернул пустой каталог → cache не трогаем
  (если NS реально опустошил каталог — оператор увидит no-products в UI
  и разберётся вручную).
- Защита от исчезновения service_id: помечаем in_stock=0, НЕ удаляем
  запись (вдруг это временно; и история ссылок shop_orders → service_id
  не битая).
- Защита от нулевых цен: services с price≤0 пропускаем (NS-баг или
  технические placeholder'ы).
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from loguru import logger

from src.config import Settings, get_settings
from src.config_runtime import get_shop_markup_percent
from src.db.session import session_factory
from src.ns.models import StockResponse
from src.shop.pricing import compute_shop_price_kopecks
from src.shop.repo import mark_services_unseen, upsert_catalog_service
from src.sync.fx import get_rate_breakdown


class _NSStockProvider(Protocol):
    """Любой объект с методом get_stock — реальный NSClient или fake."""
    async def get_stock(self) -> StockResponse: ...


async def sync_catalog_once(
    *,
    ns_client: _NSStockProvider,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """
    Один цикл синхронизации каталога.

    Returns dict со статистикой:
        status: "ok" | "empty_ns_response" | "failed"
        fetched: сколько services пришло от NS
        upserted: сколько строк добавили/обновили в cache
        skipped_invalid: пропущено из-за price<=0
        marked_oos: помечено in_stock=0 (исчезли из ответа NS)
        fx_rate: эффективный курс, использованный для расчёта
        markup_percent: использованный markup
        error: текст ошибки (только при status=failed)
    """
    settings = settings or get_settings()

    try:
        stock = await ns_client.get_stock()
    except Exception as exc:
        logger.exception(f"shop catalog: NS get_stock упал: {exc}")
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "fetched": 0,
            "upserted": 0,
            "skipped_invalid": 0,
            "marked_oos": 0,
        }

    fetched = sum(len(cat.services) for cat in stock.categories)
    if fetched == 0:
        # Защита: если NS прислал пустой ответ (network blip / временный сбой /
        # авария на стороне NS), НЕ обнуляем cache. Покупатели увидят последние
        # известные товары, а не «магазин пуст».
        logger.warning(
            "shop catalog: NS вернул пустой каталог; cache не трогаю "
            "(возможен сбой NS)"
        )
        return {
            "status": "empty_ns_response",
            "fetched": 0,
            "upserted": 0,
            "skipped_invalid": 0,
            "marked_oos": 0,
        }

    # Курс и markup — снимок на момент sync'а
    breakdown = await get_rate_breakdown(settings)
    fx_rate = breakdown.effective
    markup = await get_shop_markup_percent(settings)

    upserted = 0
    skipped_invalid = 0
    seen_ids: list[int] = []

    async with session_factory()() as session:
        for cat in stock.categories:
            fields_json = json.dumps(
                [f.model_dump() for f in cat.fields], ensure_ascii=False
            ) if cat.fields else None

            for svc in cat.services:
                if svc.price <= 0:
                    # NS-placeholder / технический сервис → не пускаем в shop
                    skipped_invalid += 1
                    continue

                try:
                    price_kopecks = compute_shop_price_kopecks(
                        ns_price_usd=svc.price,
                        fx_rate=fx_rate,
                        markup_percent=markup,
                    )
                except ValueError as exc:
                    logger.warning(
                        f"shop catalog: skip service {svc.service_id} "
                        f"({svc.service_name!r}): {exc}"
                    )
                    skipped_invalid += 1
                    continue

                await upsert_catalog_service(
                    session,
                    ns_service_id=svc.service_id,
                    category_id=cat.category_id,
                    category_name=cat.category_name,
                    service_name=svc.service_name,
                    ns_price_usd=svc.price,
                    rub_price_kopecks=price_kopecks,
                    in_stock=svc.in_stock,
                    fields_json=fields_json,
                )
                seen_ids.append(svc.service_id)
                upserted += 1

        marked_oos = await mark_services_unseen(
            session, seen_service_ids=seen_ids
        )
        await session.commit()

    logger.info(
        f"shop catalog: synced {upserted} services "
        f"(skipped {skipped_invalid}, marked_oos {marked_oos}, "
        f"fx={fx_rate:.4f}, markup={markup:.2f}%)"
    )
    return {
        "status": "ok",
        "fetched": fetched,
        "upserted": upserted,
        "skipped_invalid": skipped_invalid,
        "marked_oos": marked_oos,
        "fx_rate": fx_rate,
        "markup_percent": markup,
    }
