"""
Создание лотов FunPay по записи миграции + запись маппингов NS↔FunPay.

Безопасность по умолчанию:
  * лоты создаются НЕАКТИВНЫМИ (active=False) — покупатели не видят;
  * маппинг создаётся enabled=False (staged) — sync_stock его не трогает,
    пока ты не включишь вручную после проверки;
  * идемпотентность: услуги, у которых уже есть маппинг (по ns_service_id),
    пропускаются — повторный запуск не плодит дубли лотов;
  * --limit ограничивает размер волны; без --yes идёт dry-run.

После включения маппинга (Telegram /mappings ▶ или batch-enable)
sync_stock сам выставит цену/сток и активирует лот.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select

from src.db.models import Mapping
from src.db.repo import upsert_known_lot, upsert_mapping
from src.db.session import session_factory
from src.migrate.generator import (
    NodeSchemaIndex,
    build_creation_fields,
    compute_price_rub,
    filter_fields_to_schema,
    substitute,
    validate_entry_against_schema,
)
from src.migrate.loader import MigrationEntry, MigrationService


NS_QUANTITY_TEMPLATE = '{"quantity":"@QUANTITY"}'


@dataclass
class CreatedLot:
    ns_service_id: int
    nominal: int | None
    funpay_lot_id: int
    price_rub: float


@dataclass
class RunResult:
    created: list[CreatedLot] = field(default_factory=list)
    skipped_already_mapped: list[int] = field(default_factory=list)  # ns_service_id
    skipped_unsupported: list[int] = field(default_factory=list)     # ns_service_id
    errors: list[str] = field(default_factory=list)


def _mapping_label(entry: MigrationEntry, service: MigrationService) -> str:
    """Лейбл маппинга в NS-стиле — для UI и подстраховки матчинга заказов."""
    region = entry.region_code or entry.region_en or ""
    nominal = "" if service.nominal is None else str(service.nominal)
    cur = entry.currency or ""
    parts = [entry.platform]
    if region:
        parts.append(region)
    if nominal or cur:
        parts.append(f"{nominal} {cur}".strip())
    return " | ".join(p for p in parts if p)


async def _existing_mapped_service_ids() -> set[int]:
    async with session_factory()() as session:
        rows = (await session.execute(select(Mapping.ns_service_id))).scalars().all()
    return {int(x) for x in rows}


async def _detect_new_lot_id(
    admin: Any, node_id: int, before_ids: set[int], created_ids: set[int]
) -> int | None:
    """Новый lot_id = появившийся в списке офферов раздела id, которого не
    было до волны и который мы ещё не присвоили."""
    try:
        offers = await admin.list_node_offers(node_id)
    except Exception as exc:
        logger.warning(f"list_node_offers({node_id}) упал при детекте lot_id: {exc}")
        return None
    current = {int(o["offer_id"]) for o in offers}
    candidates = current - before_ids - created_ids
    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        return None
    # Несколько кандидатов (параллельные изменения?) — берём максимальный
    # (самый свежий id), но честно предупреждаем.
    logger.warning(
        f"Детект lot_id неоднозначен: новых кандидатов {sorted(candidates)}; "
        f"беру max."
    )
    return max(candidates)


async def run_category(
    entry: MigrationEntry,
    admin: Any,
    schema: dict[str, Any],
    fx_rate: float,
    *,
    limit: int | None = None,
    activate: bool = False,
    dry_run: bool = True,
    inter_request_delay_seconds: float = 0.8,
) -> RunResult:
    """
    Создаёт лоты по «зелёным» (валидным) услугам категории и пишет маппинги.

    activate=False (default): лот active=False, mapping enabled=False.
    activate=True: лот active=True, mapping enabled=True (сразу в продажу).
    dry_run=True: ничего не создаём, только показываем план.
    """
    result = RunResult()

    validation = validate_entry_against_schema(entry, schema, fx_rate)
    ok_services = [sv.service for sv in validation.ok_services]
    result.skipped_unsupported = [sv.service.service_id for sv in validation.bad_services]

    already = await _existing_mapped_service_ids()

    to_create: list[MigrationService] = []
    for svc in ok_services:
        if svc.service_id in already:
            result.skipped_already_mapped.append(svc.service_id)
            continue
        to_create.append(svc)
        if limit is not None and len(to_create) >= limit:
            break

    logger.info(
        f"[{entry.ns_category_id}] {entry.ns_category_name}: "
        f"к созданию {len(to_create)}, уже замаплено "
        f"{len(result.skipped_already_mapped)}, неподдерж. "
        f"{len(result.skipped_unsupported)}"
        + (" [DRY-RUN]" if dry_run else "")
    )

    if dry_run:
        for svc in to_create:
            price = compute_price_rub(svc.price_usd, entry.markup_percent, fx_rate)
            logger.info(
                f"  [dry-run] svc {svc.service_id} номинал {svc.nominal} "
                f"→ лот ({'active' if activate else 'inactive'}), цена ~{price}₽"
            )
        return result

    before_ids = {int(o["offer_id"]) for o in await admin.list_node_offers(entry.funpay_node)}
    created_ids: set[int] = set()
    schema_idx = NodeSchemaIndex.from_schema(schema)

    for svc in to_create:
        price = compute_price_rub(svc.price_usd, entry.markup_percent, fx_rate)
        try:
            lot = await admin.get_lot_fields(0, node_id=entry.funpay_node)
            fields = build_creation_fields(entry, svc, fx_rate)
            # Шлём только поля, что есть в форме раздела (у Steam Wallet нет
            # summary, у PlayStation свой набор — лишнее отбрасываем).
            fields, dropped = filter_fields_to_schema(fields, schema_idx)
            if dropped:
                logger.debug(f"  svc {svc.service_id}: поля не в форме раздела, пропущены: {dropped}")
            lot.raw_fields.update(fields)
            lot.price = price
            lot.amount = 1  # стартовый сток; sync выставит реальный
            lot.active = bool(activate)

            save_result = await admin.save_lot(lot)
            if isinstance(save_result, dict) and not save_result.get("ok"):
                raise RuntimeError(
                    f"save_lot не ok: {save_result.get('funpay_error') or save_result}"
                )

            await asyncio.sleep(0.5)  # дать FunPay проиндексировать оффер
            lot_id = await _detect_new_lot_id(
                admin, entry.funpay_node, before_ids, created_ids
            )
            if lot_id is None:
                raise RuntimeError(
                    "лот создан (save ok), но не удалось определить lot_id "
                    "через list_node_offers"
                )
            created_ids.add(lot_id)

            async with session_factory()() as session:
                await upsert_mapping(
                    session,
                    funpay_lot_id=lot_id,
                    ns_service_id=svc.service_id,
                    markup_percent=entry.markup_percent,
                    ns_fields_template=NS_QUANTITY_TEMPLATE,
                    enabled=bool(activate),
                    label=_mapping_label(entry, svc),
                    # P0-1 Фаза B: node известен при создании — пишем сразу,
                    # чтобы snapshot-sync не пришлось backfill'ить новые лоты.
                    funpay_node_id=entry.funpay_node,
                )
                # KnownLot с заголовком = подставленный summary_ru: даёт
                # matcher'у сильный сигнал title с первого же заказа без
                # lot_id (P1-4), не дожидаясь new_lots discovery.
                await upsert_known_lot(
                    session,
                    funpay_lot_id=lot_id,
                    title=substitute(entry.summary_ru, entry=entry, service=svc),
                    mark_notified=True,
                )
                await session.commit()

            result.created.append(CreatedLot(
                ns_service_id=svc.service_id,
                nominal=svc.nominal,
                funpay_lot_id=lot_id,
                price_rub=price,
            ))
            logger.success(
                f"  ✅ svc {svc.service_id} (номинал {svc.nominal}) → "
                f"lot {lot_id}, маппинг {'enabled' if activate else 'staged'}"
            )
        except Exception as exc:
            msg = f"svc {svc.service_id} (номинал {svc.nominal}): {exc}"
            logger.error(f"  ⛔ {msg}")
            result.errors.append(msg)

        if inter_request_delay_seconds > 0:
            await asyncio.sleep(inter_request_delay_seconds)

    return result
