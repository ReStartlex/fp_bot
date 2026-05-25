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
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import select

from src.config import Settings, get_settings
from src.config_runtime import get_global_markup_percent, get_stock_cap
from src.db.models import LotGroup
from src.db.repo import (
    finish_sync_run,
    list_mappings,
    reserved_quantities_by_service,
    start_sync_run,
    update_mapping_last_synced,
)
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


def _service_with_reserved_stock(service: Service, reserved: int) -> Service:
    if reserved <= 0:
        return service
    return service.model_copy(update={"in_stock": max(0, service.in_stock - reserved)})


def _risk_skip_reason(
    *,
    target: PricingResult,
    current_price: float | None,
    settings: Settings,
) -> str | None:
    cost = target.ns_price_usd * target.fx_rate
    if target.price_target <= 0:
        return "guardrail: целевая цена <= 0"
    withdrawal_fee_percent = float(
        getattr(settings, "funpay_withdrawal_fee_percent", 0.0)
    )
    withdrawal_fee = target.price_target * withdrawal_fee_percent / 100.0
    margin = (target.price_target - withdrawal_fee - cost) / target.price_target * 100.0
    if margin < settings.sync_min_margin_percent:
        return (
            "guardrail: маржа ниже минимума "
            f"({margin:.2f}% < {settings.sync_min_margin_percent:.2f}%)"
        )
    if (
        current_price is not None
        and current_price > 0
        and settings.sync_max_price_change_percent > 0
    ):
        new_price = target.round_price()
        change = abs(new_price - current_price) / current_price * 100.0
        if change > settings.sync_max_price_change_percent:
            return (
                "guardrail: слишком большое изменение цены "
                f"({change:.1f}% > {settings.sync_max_price_change_percent:.1f}%)"
            )
    return None


async def _decide_for_one(
    ns_service: Service | None,
    mapping: Any,
    settings: Settings,
    fx_rate: float,
    funpay_client: FunPayClient,
    *,
    effective_markup: float | None = None,
    effective_stock_cap: int | None = None,
    group: LotGroup | None = None,
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
        group_markup_percent=group.markup_percent if group is not None else None,
        group_stock_cap=group.stock_cap if group is not None else None,
    )

    # Читаем текущее состояние лота на FunPay
    try:
        lot_fields = await funpay_client.get_lot_fields(mapping.funpay_lot_id)
    except Exception as exc:
        text = str(exc)
        hint = ""
        # Auth-ошибка (golden_key инвалидирован FunPay'ем) — самая частая.
        if "auth" in text.lower() or "login" in text.lower():
            hint = " (обнови FUNPAY_GOLDEN_KEY в .env и перезапусти сервис)"
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

    risk_reason = _risk_skip_reason(
        target=target, current_price=current_price, settings=settings
    )
    if risk_reason is not None:
        return LotSyncDecision(
            funpay_lot_id=mapping.funpay_lot_id,
            ns_service_id=mapping.ns_service_id,
            label=mapping.label,
            current_price=current_price,
            target=target,
            will_update_price=False,
            will_update_stock=False,
            will_activate=False,
            will_deactivate=False,
            skip_reason=risk_reason,
        )

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


def _find_mapping_id_for_decision(
    mappings: list[Any], decision: "LotSyncDecision"
) -> int | None:
    """Найти Mapping.id по funpay_lot_id из decision.

    Линейный поиск — список маленький (десятки лотов), оптимизировать
    через dict не имеет смысла. Возвращает None если не нашли
    (защита от теоретического рассогласования).
    """
    for m in mappings:
        if getattr(m, "funpay_lot_id", None) == decision.funpay_lot_id:
            return getattr(m, "id", None)
    return None


def _compute_target_quickly(
    *,
    ns_service: Service | None,
    mapping: Any,
    settings: Settings,
    fx_rate: float,
    effective_markup: float,
    effective_stock_cap: int,
    group: Any | None,
) -> PricingResult | None:
    """
    Быстрый расчёт target (price, stock) ТОЛЬКО на основе NS-данных,
    без единого FunPay-запроса. Используется fast-path'ом diff-sync.

    Если ns_service отсутствует (нет в каталоге) — возвращает None,
    fast-path не применим, дальше пойдёт обычный путь, который вернёт
    skip_reason="NS service_id не найден".
    """
    if ns_service is None:
        return None
    return compute_pricing(
        ns_service=ns_service,
        mapping=mapping,
        settings=settings,
        fx_rate_usd_to_target=fx_rate,
        default_markup=effective_markup,
        default_stock_cap=effective_stock_cap,
        group_markup_percent=group.markup_percent if group is not None else None,
        group_stock_cap=group.stock_cap if group is not None else None,
    )


def _is_cache_hit(
    *,
    mapping: Any,
    target: PricingResult,
    ttl_seconds: int,
    now: datetime | None = None,
) -> bool:
    """
    True, если target совпадает с last_synced и last_synced свежий.

    Все три условия должны быть выполнены:
      1. Cache заполнен (`last_synced_at` не NULL — иначе первый run);
      2. Cache свежий: `now - last_synced_at < TTL` (защита от рассинхрона
         с FunPay, если кто-то правит цены через UI вручную);
      3. Target == cache:
         - price (округлённая): сравниваем как float с допуском 0.005
         - stock: int-сравнение
         - active: bool (производное от stock > 0)

    Допуск 0.005 на цену — потому что round_price() может дать
    разный результат после floating-point round-trip через БД.
    Stock и active — точное сравнение.
    """
    last_at = getattr(mapping, "last_synced_at", None)
    last_price = getattr(mapping, "last_synced_price", None)
    last_stock = getattr(mapping, "last_synced_stock", None)
    last_active = getattr(mapping, "last_synced_active", None)

    if last_at is None or last_price is None or last_stock is None or last_active is None:
        return False

    current_time = now or datetime.utcnow()
    if (current_time - last_at).total_seconds() >= ttl_seconds:
        return False

    target_price = target.round_price()
    target_stock = target.stock
    target_active = target.stock > 0

    if abs(float(last_price) - float(target_price)) > 0.005:
        return False
    if int(last_stock) != int(target_stock):
        return False
    if bool(last_active) != bool(target_active):
        return False

    return True


class SaveLotFailed(RuntimeError):
    """
    FunPay save_lot вернул ответ, но это НЕ подтверждённый успех.

    Бросается из `_apply_decision`, если `funpay_client.save_lot()`
    отдал `{"ok": False, ...}` — например, исчерпание 429-retries
    (rate-limit) или ошибка от самого FunPay в JSON-ответе.

    КРИТИЧНО: до этой проверки `_apply_decision` молча возвращался
    после save_lot, и `run_sync_once` инкрементил lots_updated даже
    если обновление реально не прошло. Метрики r429/exhaust помогли
    это диагностировать. Теперь fail виден как WARN + skipped++.
    """


async def _apply_decision(
    decision: LotSyncDecision,
    funpay_client: FunPayClient,
    settings: Settings,
) -> None:
    """Применить решение к лоту на FunPay.

    На fail save_lot бросает SaveLotFailed — это поймает try/except
    в run_sync_once и инкрементнёт lots_skipped, плюс залогирует
    WARN. Раньше fail save_lot молча проглатывался, и lots_updated
    врал.
    """
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

    result = await funpay_client.save_lot(lot_fields)

    # FunPayClient.save_lot может вернуть:
    #   * dict с ключом "ok": bool — наш собственный admin_http.save_lot
    #   * None (старые/мокированные клиенты) — считаем успехом
    #   * другие типы — считаем успехом (на стороне нашего admin_http
    #     это всегда dict, но защищаемся от изменения контракта).
    if isinstance(result, dict) and result.get("ok") is False:
        err = result.get("funpay_error") or result.get("json") or "unknown FunPay error"
        http_status = result.get("http_status")
        raise SaveLotFailed(
            f"save_lot({decision.funpay_lot_id}) не подтвердил успех: "
            f"http={http_status}, error={err}"
        )


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
    # diff-cache fast-path: лоты, которые НЕ потребовали FunPay GET
    # (NS-target совпадает с last_synced и last_synced свежий).
    # Это НЕ skipped — это «не было нужды трогать», т.е. желаемый
    # стабильный режим. Логируется отдельным полем в "Sync done".
    lots_unchanged = 0
    # Счётчик «capped»: лотов, у которых NS_stock > effective_cap.
    # Полезно для UX-диагностики: «99/100» после продажи может выглядеть
    # как «не синхронизируется», хотя на самом деле работает корректно —
    # просто cap=100 ограничивает выставленный stock. Высокое значение
    # capped=N намекает что юзеру стоит увеличить cap или дать per-lot cap.
    lots_capped = 0
    error: str | None = None
    # Маппинги, которые нужно обновить last_synced_* после успешного цикла
    # (только те, для которых _apply_decision прошёл без исключения).
    pending_cache_updates: list[tuple[int, float, int, bool]] = []

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
            group_ids = {m.group_id for m in mappings if m.group_id is not None}
            groups_by_id: dict[int, LotGroup] = {}
            if group_ids:
                result = await session.execute(
                    select(LotGroup).where(LotGroup.id.in_(group_ids))
                )
                groups_by_id = {g.id: g for g in result.scalars().all()}
            reserved_by_service = (
                await reserved_quantities_by_service(session)
                if settings.sync_reserve_pending_orders
                else {}
            )

        if not mappings:
            logger.debug("Маппингов нет — нечего синхронизировать.")
            async with session_factory()() as session:
                await finish_sync_run(
                    session, run, status="completed",
                    lots_checked=0, lots_updated=0, lots_skipped=0,
                )
                await session.commit()
            return {"checked": 0, "unchanged": 0, "updated": 0, "skipped": 0}

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

        # Готовим diff-cache параметры заранее (читаем из settings один раз).
        diff_cache_enabled = getattr(settings, "sync_stock_diff_cache_enabled", True)
        diff_cache_ttl = int(getattr(settings, "sync_stock_diff_cache_ttl_seconds", 300))
        cache_check_now = datetime.utcnow()  # фиксируем "now" для всех проверок цикла

        decisions: list[LotSyncDecision] = []

        for mapping in mappings:
            ns_service = services_index.get(mapping.ns_service_id)
            if ns_service is not None:
                reserved = reserved_by_service.get(mapping.ns_service_id, 0)
                ns_service = _service_with_reserved_stock(ns_service, reserved)

            # === Diff-cache fast-path ===
            # Если NS-target совпадает с last_synced и last_synced свежий —
            # пропускаем FunPay-запрос полностью (главный источник 429-нагрузки).
            if diff_cache_enabled:
                quick_target = _compute_target_quickly(
                    ns_service=ns_service,
                    mapping=mapping,
                    settings=settings,
                    fx_rate=fx_rate,
                    effective_markup=effective_markup,
                    effective_stock_cap=effective_stock_cap,
                    group=(
                        groups_by_id.get(mapping.group_id)
                        if mapping.group_id is not None else None
                    ),
                )
                if quick_target is not None and _is_cache_hit(
                    mapping=mapping,
                    target=quick_target,
                    ttl_seconds=diff_cache_ttl,
                    now=cache_check_now,
                ):
                    label = mapping.label or f"lot {mapping.funpay_lot_id}"
                    logger.debug(
                        f"  [{label}] cache hit (price={quick_target.round_price()}, "
                        f"stock={quick_target.stock}) — skip FunPay"
                    )
                    lots_unchanged += 1
                    # ВАЖНО: НЕ обновляем last_synced_at при cache-hit!
                    # TTL должен срабатывать честно — это гарантия того,
                    # что мы периодически переоткалибруем кеш с реальным
                    # FunPay-стоком. Иначе FunPay сам при продаже снижает
                    # сток (100→97), наш target всё ещё 100 (cap), cache
                    # видит совпадение и пропускает sync навсегда —
                    # см. инцидент 2026-05-25.
                    continue

            # === Обычный путь: FunPay GET для проверки текущего состояния ===
            decision = await _decide_for_one(
                ns_service, mapping, settings, fx_rate, funpay_client,
                effective_markup=effective_markup,
                effective_stock_cap=effective_stock_cap,
                group=groups_by_id.get(mapping.group_id)
                if mapping.group_id is not None
                else None,
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

            # Cap-аннотация: если NS возвращает stock больше нашего cap'а,
            # дописываем подсказку «capped: NS=N>cap=K» в action_str.
            # Это полезно при диагностике «висит на 99» — сразу видно
            # что это работа cap'а, а не баг синхронизации.
            # Поле в Service называется in_stock (а не stock); запас на
            # случай миграции схемы — оба варианта.
            ns_service_for_log = services_index.get(decision.ns_service_id)
            if ns_service_for_log is not None:
                raw_ns_stock = int(
                    getattr(ns_service_for_log, "in_stock", None)
                    or getattr(ns_service_for_log, "stock", 0)
                    or 0
                )
                if raw_ns_stock > decision.target.stock and decision.target.stock > 0:
                    lots_capped += 1
                    action_str = (
                        f"{action_str} (capped: NS={raw_ns_stock}>cap={decision.target.stock})"
                    )

            if decision.skip_reason:
                logger.warning(f"  [{label}] SKIP: {decision.skip_reason}")
                lots_skipped += 1
                continue

            if not actions:
                # Verified no-action: FunPay-GET подтвердил, что
                # current_price/stock на FunPay уже == target. Это идеальный
                # момент заполнить diff-cache: на следующем цикле fast-path
                # увидит совпадение и пропустит FunPay-GET совсем.
                #
                # КРИТИЧНО: этот блок ДОЛЖЕН быть ДО `continue`, иначе
                # cache не наполнится никогда (только что нашёл этот баг
                # в проде — `unchanged=1` всегда, потому что cache
                # обновлялся только при save_lot success, а save_lot'ов
                # в стабильном состоянии 0). Без этого fast-path
                # бесполезен — все лоты вечно cache miss.
                logger.debug(f"  [{label}] {action_str}")
                mapping_id = _find_mapping_id_for_decision(mappings, decision)
                if mapping_id is not None and decision.current_price is not None:
                    pending_cache_updates.append((
                        mapping_id,
                        decision.target.round_price(),
                        decision.target.stock,
                        decision.target.stock > 0,
                    ))
                continue

            if dry_run:
                logger.info(f"  [{label}] DRY-RUN: {action_str}")
                lots_updated += 1
            else:
                try:
                    await _apply_decision(decision, funpay_client, settings)
                    logger.success(f"  [{label}] applied: {action_str}")
                    lots_updated += 1
                    # diff-cache: после успешного save_lot запоминаем
                    # «новое» равновесие. КРИТИЧНО: только при success.
                    # Если save_lot fail'нул (SaveLotFailed) — last_synced
                    # НЕ обновляем, чтобы на следующем цикле retry прошёл
                    # через нормальный путь (а не cache-hit).
                    mapping_id = _find_mapping_id_for_decision(mappings, decision)
                    if mapping_id is not None:
                        pending_cache_updates.append((
                            mapping_id,
                            decision.target.round_price(),
                            decision.target.stock,
                            decision.target.stock > 0,
                        ))
                except Exception as exc:
                    logger.exception(f"  [{label}] update FAILED: {exc}")
                    lots_skipped += 1
                # Не спамим FunPay: пауза согласно rate-limit
                await asyncio.sleep(1.0 / settings.funpay_update_rate_limit_per_second)

        # === Сохранение diff-cache: только успешные save_lot + verified-no-action ===
        # pending_cache_updates наполняется в двух местах:
        #   1. После успешного save_lot (мы знаем что FunPay теперь == target)
        #   2. После verified no-action (FunPay-GET подтвердил FunPay уже == target)
        # Cache-hits (fast-path) сюда НЕ попадают — их last_synced_at должен
        # истекать честно через TTL, чтобы периодически переоткалибровываться.
        if pending_cache_updates:
            try:
                async with session_factory()() as session:
                    for mid, price, stock, active in pending_cache_updates:
                        await update_mapping_last_synced(
                            session,
                            mapping_id=mid,
                            price=price,
                            stock=stock,
                            active=active,
                        )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                # Cache — это оптимизация, её падение не должно ломать sync.
                # Хуже всего: следующий цикл сделает лишний FunPay GET — не катастрофа.
                logger.warning(f"diff-cache: не удалось обновить last_synced: {exc}")

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

    # Снимаем HTTP-метрики FunPay за прошедший цикл (атомарно сбрасываются
    # в admin-клиенте). Это даёт прямую видимость работы rate-limiter'a и
    # retry-логики в проде, без необходимости grep'ать journalctl.
    # `exhausted` > 0 — это уже инцидент (лот пропущен, нужен внимание).
    http_metrics: dict[str, int] = {"ok": 0, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}
    if funpay_client is not None:
        try:
            http_metrics = funpay_client.get_and_reset_http_metrics()
        except Exception as exc:  # noqa: BLE001
            # метрики — это observability, они не должны ломать sync_stock
            logger.debug(f"Sync done: не удалось снять http-метрики: {exc}")

    http_str = (
        f"http=[ok={http_metrics['ok']} "
        f"r429={http_metrics['retry_429']} "
        f"r5xx={http_metrics['retry_5xx']} "
        f"fails={http_metrics['exhausted']}]"
    )

    # Total = checked + unchanged: для оператора видно сколько маппингов
    # реально обработано (включая cache-hits, которые не идут в `checked`
    # потому что не было «решения»).
    total_mappings = lots_checked + lots_unchanged

    # `capped=N` показываем только если N>0, чтобы не засорять обычные
    # строки. Конкретные имена capped-лотов не пишем — это видно
    # построчно через action_str «(capped: NS=N>cap=K)».
    capped_suffix = f", capped={lots_capped}" if lots_capped > 0 else ""

    if total_mappings > 0 or error:
        # На exhausted'ы хотим обращать внимание — повышаем уровень до WARNING.
        line = (
            f"Sync done: checked={lots_checked}, "
            f"unchanged={lots_unchanged}, "
            f"updated={lots_updated}, skipped={lots_skipped}{capped_suffix}, {http_str}"
        )
        if http_metrics["exhausted"] > 0:
            logger.warning(line + "  (есть исчерпания retry — лоты пропущены!)")
        else:
            logger.info(line)
    else:
        logger.debug(
            f"Sync done (empty): checked={lots_checked}, "
            f"unchanged={lots_unchanged}, "
            f"updated={lots_updated}, skipped={lots_skipped}{capped_suffix}, {http_str}"
        )
    return {
        "checked": lots_checked,
        "unchanged": lots_unchanged,
        "updated": lots_updated,
        "skipped": lots_skipped,
        "capped": lots_capped,
        "http": http_metrics,
    }
