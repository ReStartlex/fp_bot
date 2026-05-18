"""
Синхронизация каталога NS -> FunPay.

Что делает один прогон:
1. Тянет /stock у NS — все категории/услуги.
2. Достаёт из БД список mappings.
3. Для каждого mapping:
   - находит соответствующий NS-сервис
   - считает целевую цену (с markup, конверсией USD->RUB) и сток
   - читает текущее состояние лота на FunPay
   - если цена/сток изменились — обновляет лот
4. В dry-run режиме (ENABLE_REAL_ACTIONS=false) только логирует, ничего не пишет.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.config import Settings, get_settings
from src.config_runtime import get_global_markup_percent, get_stock_cap
from src.db.repo import finish_sync_run, list_mappings, start_sync_run
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.mapping.rules import PricingResult, compute_pricing, should_update_price
from src.ns import NSClient
from src.ns.models import Service, StockResponse
from src.sync.fx import get_usd_rub_rate


@dataclass
class LotSyncDecision:
    funpay_lot_id: int
    ns_service_id: int
    label: str | None
    current_price: float | None
    target: PricingResult
    will_update_price: bool
    will_update_stock: bool
    will_activate: bool
    will_deactivate: bool
    skip_reason: str | None = None


def _flatten_services(stock: StockResponse) -> dict[int, Service]:
    """service_id -> Service из всего каталога."""
    out: dict[int, Service] = {}
    for cat in stock.categories:
        for svc in cat.services:
            out[svc.service_id] = svc
    return out


async def _decide_for_one(
    ns_service: Service | None,
    mapping: Any,
    settings: Settings,
    fx_rate: float,
    funpay_client: FunPayClient,
    *,
    effective_markup: float | None = None,
    effective_stock_cap: int | None = None,
) -> LotSyncDecision | None:
    """Решить что делать с конкретным лотом."""
    if ns_service is None:
        return LotSyncDecision(
            funpay_lot_id=mapping.funpay_lot_id,
            ns_service_id=mapping.ns_service_id,
            label=mapping.label,
            current_price=None,
            target=PricingResult(0, fx_rate, 0, 0, 0, settings.funpay_currency),
            will_update_price=False,
            will_update_stock=False,
            will_activate=False,
            will_deactivate=False,
            skip_reason="NS service_id не найден в каталоге",
        )

    target = compute_pricing(
        ns_service=ns_service,
        mapping=mapping,
        settings=settings,
        fx_rate_usd_to_target=fx_rate,
        default_markup=effective_markup,
        default_stock_cap=effective_stock_cap,
    )

    # Читаем текущее состояние лота на FunPay
    try:
        lot_fields = await funpay_client.get_lot_fields(mapping.funpay_lot_id)
    except Exception as exc:
        text = str(exc)
        # Подсказка по самой частой проблеме
        hint = ""
        if "expecting value" in text.lower():
            hint = (
                " (вероятно протух FUNPAY_PHPSESSID — обнови в .env и "
                "перезапусти сервис)"
            )
        return LotSyncDecision(
            funpay_lot_id=mapping.funpay_lot_id,
            ns_service_id=mapping.ns_service_id,
            label=mapping.label,
            current_price=None,
            target=target,
            will_update_price=False,
            will_update_stock=False,
            will_activate=False,
            will_deactivate=False,
            skip_reason=f"FunPay get_lot_fields упал: {exc}{hint}",
        )

    current_price = _extract_price(lot_fields)
    current_stock = _extract_stock(lot_fields)
    current_active = _extract_active(lot_fields)

    new_price = target.round_price()
    new_stock = target.stock

    will_update_price = should_update_price(
        current_price, new_price, settings.price_update_threshold_percent
    )
    will_update_stock = current_stock != new_stock
    will_deactivate = current_active and new_stock == 0
    will_activate = (not current_active) and new_stock > 0

    return LotSyncDecision(
        funpay_lot_id=mapping.funpay_lot_id,
        ns_service_id=mapping.ns_service_id,
        label=mapping.label,
        current_price=current_price,
        target=target,
        will_update_price=will_update_price,
        will_update_stock=will_update_stock,
        will_activate=will_activate,
        will_deactivate=will_deactivate,
    )


def _extract_price(lot_fields: Any) -> float | None:
    """Достать текущую цену из FunPay LotFields (имена полей могут варьироваться)."""
    for attr in ("price", "fields", "_fields"):
        value = getattr(lot_fields, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for key in ("price", "цена", "fields[price]"):
                v = value.get(key)
                if v is not None:
                    try:
                        return float(str(v).replace(",", "."))
                    except ValueError:
                        continue
    return None


def _extract_stock(lot_fields: Any) -> int:
    for attr in ("amount", "stock", "quantity"):
        value = getattr(lot_fields, attr, None)
        if isinstance(value, int):
            return value
    fields = getattr(lot_fields, "fields", None)
    if isinstance(fields, dict):
        for key in ("amount", "quantity", "fields[amount]"):
            v = fields.get(key)
            if v is not None:
                try:
                    return int(v)
                except ValueError:
                    continue
    return 0


def _extract_active(lot_fields: Any) -> bool:
    for attr in ("active", "is_active", "active_lot"):
        value = getattr(lot_fields, attr, None)
        if isinstance(value, bool):
            return value
    return True  # консервативно считаем активным


async def _apply_decision(
    decision: LotSyncDecision,
    funpay_client: FunPayClient,
    settings: Settings,
) -> None:
    """Применить решение к лоту на FunPay."""
    lot_fields = await funpay_client.get_lot_fields(decision.funpay_lot_id)

    # Принципы безопасной мутации: пишем только в известные атрибуты
    if decision.will_update_price:
        _set_price(lot_fields, decision.target.round_price())
    if decision.will_update_stock or decision.will_activate or decision.will_deactivate:
        _set_stock(lot_fields, decision.target.stock)
    if decision.will_deactivate:
        _set_active(lot_fields, False)
    elif decision.will_activate:
        _set_active(lot_fields, True)

    await funpay_client.save_lot(lot_fields)


def _set_price(lot_fields: Any, price: float) -> None:
    for attr in ("price",):
        if hasattr(lot_fields, attr):
            setattr(lot_fields, attr, price)
            return
    fields = getattr(lot_fields, "fields", None)
    if isinstance(fields, dict):
        fields["price"] = str(price)


def _set_stock(lot_fields: Any, stock: int) -> None:
    for attr in ("amount", "stock", "quantity"):
        if hasattr(lot_fields, attr):
            setattr(lot_fields, attr, stock)
            return
    fields = getattr(lot_fields, "fields", None)
    if isinstance(fields, dict):
        fields["amount"] = str(stock)


def _set_active(lot_fields: Any, active: bool) -> None:
    for attr in ("active", "is_active"):
        if hasattr(lot_fields, attr):
            setattr(lot_fields, attr, active)
            return


async def sync_once(
    *,
    dry_run: bool | None = None,
    funpay_client: FunPayClient | None = None,
    ns_client: NSClient | None = None,
) -> dict[str, int]:
    """
    Один прогон синхронизации.
    Если ENABLE_REAL_ACTIONS=false или dry_run=True — изменения только логируются.
    """
    settings = get_settings()
    if dry_run is None:
        dry_run = not settings.enable_real_actions

    logger.debug(f"Sync run started (dry_run={dry_run})")

    async with session_factory()() as session:
        run = await start_sync_run(session)
        await session.commit()

    lots_checked = 0
    lots_updated = 0
    lots_skipped = 0
    error: str | None = None

    own_ns = ns_client is None
    own_fp = funpay_client is None
    if own_ns:
        ns_client = NSClient()
        await ns_client.__aenter__()
    if own_fp:
        funpay_client = FunPayClient()
        await funpay_client.__aenter__()
        await funpay_client.connect()

    try:
        async with session_factory()() as session:
            mappings = await list_mappings(session, only_enabled=True)

        if not mappings:
            logger.debug("Маппингов нет — нечего синхронизировать.")
            async with session_factory()() as session:
                await finish_sync_run(
                    session, run, status="completed",
                    lots_checked=0, lots_updated=0, lots_skipped=0,
                )
                await session.commit()
            return {"checked": 0, "updated": 0, "skipped": 0}

        stock = await ns_client.get_stock()
        services_index = _flatten_services(stock)
        fx_rate = await get_usd_rub_rate(settings)
        effective_markup = await get_global_markup_percent(settings)
        effective_stock_cap = await get_stock_cap(settings)
        logger.info(
            f"Sync: маппингов {len(mappings)}, USD/RUB {fx_rate:.4f}, "
            f"markup default {effective_markup:.2f}%, "
            f"stock_cap default {effective_stock_cap}"
        )

        decisions: list[LotSyncDecision] = []
        for mapping in mappings:
            ns_service = services_index.get(mapping.ns_service_id)
            decision = await _decide_for_one(
                ns_service, mapping, settings, fx_rate, funpay_client,
                effective_markup=effective_markup,
                effective_stock_cap=effective_stock_cap,
            )
            if decision is not None:
                decisions.append(decision)

        for decision in decisions:
            lots_checked += 1
            actions = []
            if decision.will_update_price:
                actions.append(
                    f"price {decision.current_price} -> {decision.target.round_price()} "
                    f"{decision.target.currency.value}"
                )
            if decision.will_update_stock:
                actions.append(f"stock -> {decision.target.stock}")
            if decision.will_activate:
                actions.append("activate")
            if decision.will_deactivate:
                actions.append("deactivate")
            action_str = ", ".join(actions) if actions else "no changes"
            label = decision.label or f"lot {decision.funpay_lot_id}"

            if decision.skip_reason:
                logger.warning(f"  [{label}] SKIP: {decision.skip_reason}")
                lots_skipped += 1
                continue

            if not actions:
                logger.debug(f"  [{label}] {action_str}")
                continue

            if dry_run:
                logger.info(f"  [{label}] DRY-RUN: {action_str}")
                lots_updated += 1
            else:
                try:
                    await _apply_decision(decision, funpay_client, settings)
                    logger.success(f"  [{label}] applied: {action_str}")
                    lots_updated += 1
                except Exception as exc:
                    logger.exception(f"  [{label}] update FAILED: {exc}")
                    lots_skipped += 1
                # Не спамим FunPay: пауза согласно rate-limit
                await asyncio.sleep(1.0 / settings.funpay_update_rate_limit_per_second)

    except Exception as exc:
        logger.exception(f"Sync run упал: {exc}")
        error = str(exc)
    finally:
        if own_ns and ns_client is not None:
            await ns_client.__aexit__(None, None, None)
        if own_fp and funpay_client is not None:
            await funpay_client.__aexit__(None, None, None)

    async with session_factory()() as session:
        await finish_sync_run(
            session,
            run,
            status="failed" if error else "completed",
            lots_checked=lots_checked,
            lots_updated=lots_updated,
            lots_skipped=lots_skipped,
            error=error,
        )
        await session.commit()

    if lots_checked > 0 or error:
        logger.info(
            f"Sync done: checked={lots_checked}, "
            f"updated={lots_updated}, skipped={lots_skipped}"
        )
    else:
        logger.debug(
            f"Sync done (empty): checked={lots_checked}, "
            f"updated={lots_updated}, skipped={lots_skipped}"
        )
    return {"checked": lots_checked, "updated": lots_updated, "skipped": lots_skipped}
