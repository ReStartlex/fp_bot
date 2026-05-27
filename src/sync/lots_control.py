"""Batch enable/disable всех замапленных лотов одним вызовом.

Зачем
=====

Когда NS.gifts лежит, владельцу нужно одной кнопкой снять с продажи ВСЕ
лоты, чтобы покупатели не нарывались на failed-заказы. И, симметрично,
вернуть их обратно после восстановления.

Раньше это делалось через UI «по одному лоту», что:
  * долго (30+ маппингов × 2 клика);
  * легко забыть какой-то лот;
  * sync_stock на следующем цикле всё равно мог «починить» вкл/выкл
    в зависимости от состояния mapping.enabled, и конечный результат
    отличался от ожидания.

Что делает этот модуль
======================

* ``disable_all_mapped_lots`` — для каждого замапленного лота:
    1. ``mapping.enabled = False`` в БД (источник правды для sync_stock).
    2. ``save_lot(active=False, amount=0)`` на FunPay.
  Если save_lot упал — лот всё равно остаётся в БД disabled, его
  потом подберёт ``zombie_lot_reaper`` (он специально для этого
  случая).

* ``enable_all_mapped_lots`` — симметрично:
    1. ``mapping.enabled = True`` в БД (даже если ранее был disabled —
       пользователь явно попросил «включи всё»).
    2. ``save_lot(active=True, amount=…)`` с актуальным stock'ом из NS
       (через текущий sync-pipeline, чтобы не вычислять цены здесь
       вручную). На практике достаточно просто включить mapping в БД,
       а следующий тик ``sync_stock`` (≤30с) сам сделает save_lot с
       правильными ценой/стоком.

Гарантии безопасности
=====================

* Идемпотентность: повторный вызов с тем же state ничего не сломает
  (save_lot с теми же значениями FunPay принимает молча).
* Защита от race с sync_stock: оба воркера держат свой prepared list
  и работают через ту же таблицу mappings — конкуренции за строки нет,
  SQLite WAL разрулит.
* Не падаем наружу: на каждой ошибке считаем `errors+=1` и продолжаем.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select, update as sa_update

from src.db.models import Mapping
from src.db.repo import invalidate_mapping_cache_for_funpay_lot
from src.db.session import session_factory
from src.funpay.client import FunPayClient


@dataclass
class BatchLotResult:
    """Метрики одного прогона batch enable/disable."""
    total: int = 0                # сколько маппингов вообще нашли
    db_updated: int = 0           # сколько строк mapping.enabled поменяли в БД
    funpay_changed: int = 0       # сколько save_lot реально успешно прошли
    funpay_already: int = 0       # FunPay-лот уже в нужном состоянии
    errors: int = 0               # сколько save_lot/get_lot_fields упали
    error_lot_ids: list[int] = field(default_factory=list)


async def _set_all_mappings_enabled(
    *,
    target_enabled: bool,
) -> list[Mapping]:
    """Атомарно обновляет mapping.enabled и возвращает список ВСЕХ маппингов.

    Запросы делаем за одну транзакцию: сначала UPDATE, потом SELECT —
    SQLite видит изменения в той же сессии, и мы получаем актуальное
    состояние без второй транзакции.
    """
    async with session_factory()() as session:
        await session.execute(
            sa_update(Mapping)
            .where(Mapping.enabled.is_(not target_enabled))
            .values(enabled=bool(target_enabled))
        )
        await session.commit()

        stmt = select(Mapping).order_by(Mapping.funpay_lot_id)
        mappings = list((await session.execute(stmt)).scalars().all())
        return mappings


def _is_lot_in_state(lot_fields: object, *, active: bool) -> bool:
    """Проверяем: лот уже в целевом active-state?

    Для disable ещё дополнительно требуем amount=0 (как _emergency_disable_lot
    и zombie_reaper). Для enable amount не трогаем — sync_stock сам выставит
    его на следующем тике.
    """
    current_active = bool(getattr(lot_fields, "active", False))
    if active:
        return current_active
    amount = getattr(lot_fields, "amount", 0)
    try:
        amount_int = int(amount) if amount is not None else 0
    except (TypeError, ValueError):
        amount_int = 0
    return (not current_active) and amount_int == 0


async def disable_all_mapped_lots(
    *,
    funpay_client: FunPayClient | None,
    inter_request_delay_seconds: float = 0.4,
) -> BatchLotResult:
    """Одним вызовом отключает ВСЕ замапленные лоты в БД и на FunPay.

    Шаги
    -----
    1. ``mapping.enabled = False`` для всех маппингов (один UPDATE).
    2. По очереди для каждого: ``save_lot(active=False, amount=0)``.
    3. Между save_lot — задержка 400ms, чтобы не вызвать 429 от FunPay.

    Если save_lot упал — лот всё равно остаётся в БД disabled, чтобы
    ``sync_stock`` не «починил» его обратно. Зависшие в half-disabled
    состоянии лоты подберёт ``zombie_lot_reaper``.

    Параметры
    ---------
    funpay_client:
        Если None — обновляем только БД, save_lot не вызывается.
    inter_request_delay_seconds:
        Пауза между save_lot. По дефолту 400ms (соответствует
        ``funpay_save_lot_min_interval_ms`` в проде).

    Возвращает ``BatchLotResult`` с метриками.
    """
    mappings = await _set_all_mappings_enabled(target_enabled=False)
    result = BatchLotResult(total=len(mappings), db_updated=len(mappings))

    if not mappings or funpay_client is None:
        return result

    for mapping in mappings:
        lot_id = mapping.funpay_lot_id
        try:
            lot_fields = await funpay_client.get_lot_fields(lot_id)
        except Exception as exc:
            logger.warning(
                f"batch disable: get_lot_fields({lot_id}) упал: {exc}"
            )
            result.errors += 1
            result.error_lot_ids.append(lot_id)
            continue

        if _is_lot_in_state(lot_fields, active=False):
            result.funpay_already += 1
            continue

        try:
            if hasattr(lot_fields, "active"):
                lot_fields.active = False
            if hasattr(lot_fields, "amount"):
                lot_fields.amount = 0
            save_result = await funpay_client.save_lot(lot_fields)
            if isinstance(save_result, dict) and save_result.get("ok") is False:
                raise RuntimeError(
                    f"save_lot вернул ok=False: "
                    f"{save_result.get('funpay_error') or save_result}"
                )
            result.funpay_changed += 1
        except Exception as exc:
            logger.warning(
                f"batch disable: save_lot({lot_id}) упал: {exc}"
            )
            result.errors += 1
            result.error_lot_ids.append(lot_id)
            continue

        if inter_request_delay_seconds > 0:
            await asyncio.sleep(inter_request_delay_seconds)

    return result


async def enable_all_mapped_lots(
    *,
    funpay_client: FunPayClient | None,
) -> BatchLotResult:
    """Одним вызовом включает ВСЕ замапленные лоты обратно.

    В отличие от disable, здесь мы НЕ дёргаем save_lot напрямую с
    хардкодными amount/price: цена и сток зависят от NS-каталога и
    диапазона скидок, и пересчитывать их вручную — путь к расхождениям
    с sync_stock. Вместо этого мы только:

      1. ``mapping.enabled = True`` для ВСЕХ маппингов (включая
         тех, что были disabled).
      2. Инвалидируем diff-cache (last_synced_at = NULL), чтобы
         ближайший ``sync_stock`` (≤30с) пересчитал и сам включил
         каждый лот с актуальной ценой/стоком и amount.

    То есть «жёлтая лампочка» включается мгновенно, а реальная
    активация на FunPay — в течение одного sync-цикла.

    На случай, если пользователю важно получить активные лоты прямо
    сейчас и подождать он не готов — sync_stock запускается каждые 30с
    по умолчанию, и кнопка «🔄 Sync now» в меню запустит его руками.
    """
    mappings = await _set_all_mappings_enabled(target_enabled=True)
    result = BatchLotResult(total=len(mappings), db_updated=len(mappings))

    if not mappings:
        return result

    # Сбрасываем diff-cache всем замапленным лотам разом, чтобы
    # ближайший sync-цикл не пропустил их по fast-path.
    async with session_factory()() as session:
        for mapping in mappings:
            try:
                await invalidate_mapping_cache_for_funpay_lot(
                    session, funpay_lot_id=mapping.funpay_lot_id
                )
            except Exception as exc:
                logger.warning(
                    f"batch enable: invalidate_cache({mapping.funpay_lot_id}) "
                    f"упал: {exc}"
                )
                result.errors += 1
                result.error_lot_ids.append(mapping.funpay_lot_id)
        await session.commit()

    # funpay_changed/funpay_already не считаем — sync_stock сам поднимет
    # реальные цены/сток, мы только подготовили БД. См. docstring выше.
    return result
