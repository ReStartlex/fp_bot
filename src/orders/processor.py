"""
Pipeline обработки FunPay-заказа → NS-покупка → доставка кодов.

Принципы:

1. **Идемпотентность**. Безопасно вызвать несколько раз с тем же
   `funpay_order_id`. Состояние хранится в БД, при повторном входе
   функция продолжает с того шага, на котором остановилась.

2. **Защита от двойной обработки**. На каждый `funpay_order_id` берётся
   per-key asyncio.Lock — вторая параллельная обработка ждёт первую.

3. **Разделение «оплачено в NS» и «доставлено клиенту»**. Если NS
   списал деньги и вернул pins, но FunPay-чат недоступен — pins
   сохраняются в БД, статус становится `pins_ready`. При следующем
   входе (вручную, или повторно из watcher'а) функция повторит
   только доставку, не дёргая NS заново. Деньги уже списаны — пины
   обязаны дойти до клиента.

Статусы Order.status:
    received      — занесли заказ в БД, маппинг найден
    ns_created    — NS create_order успешно (списания нет)
    ns_paid       — NS pay_order успешно, ждём pins
    pins_ready    — pins получены, ещё не доставлены клиенту
    delivered     — пины уже у клиента в чате FunPay
    failed        — нельзя продолжить (нет маппинга / отказ NS / тайм-аут)
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from src.timeutil import utcnow

from loguru import logger

from src.alerts.telegram import TelegramNotifier
from src.chat import templates
from src.config import Settings, get_settings
from src.db.models import Order
from src.db.repo import (
    create_order,
    find_order_by_funpay_id,
    update_order,
)
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.ns import NSClient
from src.orders.events import FunPayOrderEvent
from src.orders.stages.resolve import (
    AmbiguousMatch,
    _resolve_chat_id,
    _resolve_mapping,
)
from src.orders.stages.common import _order_age_seconds, _pins_from_order
from src.orders.stages.holds import (
    _emergency_disable_lot,
    _mark_failed,
    _trigger_manual_hold,
)
from src.orders.stages.delivery import _deliver_pins, _should_hold_delivery
from src.orders.stages.purchase import (
    _build_ns_fields,
    _is_valid_uuid4,
    run_purchase,
)


# Per-key мьютекс: гарантирует, что одновременно над одним заказом
# работает только одна корутина (на этот процесс).
_order_locks: dict[str, asyncio.Lock] = {}


def _lock_for(funpay_order_id: str) -> asyncio.Lock:
    lock = _order_locks.get(funpay_order_id)
    if lock is None:
        lock = asyncio.Lock()
        _order_locks[funpay_order_id] = lock
    return lock


def _is_hard_timeout(
    order: Order, settings: Settings, *, now: datetime | None = None
) -> bool:
    """True если истёк жёсткий лимит на полный цикл received→delivered."""
    limit = settings.order_delivery_hard_timeout_seconds
    if limit <= 0:
        return False
    return _order_age_seconds(order, now=now) >= limit


async def process_funpay_order(
    event: FunPayOrderEvent,
    *,
    settings: Settings | None = None,
    ns_client: NSClient | None = None,
    funpay_client: FunPayClient | None = None,
    telegram: TelegramNotifier | None = None,
    dry_run: bool | None = None,
    force_delivery: bool = False,
) -> dict:
    """
    Главный pipeline. Идемпотентный и сериализованный по funpay_order_id.
    """
    settings = settings or get_settings()
    if dry_run is None:
        dry_run = not settings.enable_real_actions

    log = logger.bind(funpay_order_id=event.funpay_order_id)

    async with _lock_for(event.funpay_order_id):
        return await _process_locked(
            event, settings, ns_client, funpay_client, telegram, dry_run, log,
            force_delivery=force_delivery,
        )


async def _process_locked(
    event: FunPayOrderEvent,
    settings: Settings,
    ns_client: NSClient | None,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    dry_run: bool,
    log,
    *,
    force_delivery: bool = False,
) -> dict:
    if event.chat_id is None:
        resolved_chat_id = await _resolve_chat_id(event, funpay_client, log)
        if resolved_chat_id is not None:
            event = replace(event, chat_id=resolved_chat_id)

    log.info(
        f"Обработка FunPay-заказа: lot={event.funpay_lot_id}, "
        f"qty={event.quantity}, buyer={event.buyer_username}, "
        f"chat={event.chat_id}, dry_run={dry_run}"
    )

    # ─── 1. Быстрый выход для уже доставленных ───
    async with session_factory()() as session:
        existing = await find_order_by_funpay_id(session, event.funpay_order_id)
    if existing is not None and existing.status == "delivered":
        log.info("Заказ уже доставлен, выхожу")
        return {
            "status": "delivered",
            "skipped": True,
            "ns_custom_id": existing.ns_custom_id,
        }
    if existing is not None and existing.status == "manual_hold" and not force_delivery:
        log.warning("Заказ на ручной проверке, автоматическую выдачу не продолжаю")
        return {
            "status": "manual_hold",
            "skipped": True,
            "reason": existing.error or "manual hold",
            "ns_custom_id": existing.ns_custom_id,
        }

    # ─── 2. Доставка только-pins (без обращения к NS) ───
    # Случай: pins_ready — деньги списаны, коды есть, но send_message
    # клиенту в прошлый раз упал. Просто повторяем доставку.
    if existing is not None and existing.status in ("pins_ready", "manual_hold"):
        pins = _pins_from_order(existing)
        if pins:
            log.warning(
                f"Повторная доставка {existing.status}: {len(pins)} код(а/ов)"
            )
            return await _deliver_pins(
                event, existing, pins, funpay_client, telegram, log,
                force_delivery=force_delivery,
                help_grace_seconds=int(settings.chat_help_auto_delivery_grace_seconds),
            )
        log.error("pins_ready без pins_json — пометить failed")
        await _mark_failed(
            existing.id, "pins_ready без сохранённых pins",
            telegram, event,
            funpay_client=funpay_client,
            funpay_lot_id=existing.funpay_lot_id,
            log=log,
        )
        return {"status": "failed", "reason": "pins_ready без pins"}

    # ─── 3. Маппинг ───
    mapping = await _resolve_mapping(event, log, settings=settings)

    if isinstance(mapping, AmbiguousMatch):
        # Описание похоже сразу на несколько маппингов. НЕ угадываем:
        # ложный матч = покупка не того товара на NS. Заказ — в
        # manual_hold, оператор выбирает руками и выдаёт через NS-кабинет.
        candidates_text = "\n  ".join(mapping.candidates)
        reason = (
            f"неоднозначное сопоставление по описанию: {mapping.reason}; "
            f"кандидаты: {', '.join(mapping.candidates)}"
        )
        log.error(reason)
        async with session_factory()() as session:
            order = existing or await create_order(
                session,
                funpay_order_id=event.funpay_order_id,
                funpay_lot_id=event.funpay_lot_id or 0,
                ns_service_id=0,
                buyer_username=event.buyer_username,
                buyer_user_id=event.buyer_user_id,
                chat_id=event.chat_id,
                quantity=event.quantity,
                funpay_price_rub=event.funpay_price_rub,
                description=event.description,
            )
            await update_order(session, order, status="manual_hold", error=reason)
            await session.commit()
        if telegram is not None:
            try:
                await telegram.manual_hold_required(
                    funpay_order_id=event.funpay_order_id,
                    stage="resolve_mapping",
                    age_seconds=0,
                    buyer_username=event.buyer_username,
                    ns_custom_id=None,
                    has_pins=False,
                    reason=(
                        f"описание заказа похоже сразу на несколько лотов "
                        f"({mapping.reason}):\n  {candidates_text}\n"
                        f"description={event.description!r}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — алерт не критичен
                log.warning(f"Не смог отправить manual_hold alert: {exc}")
        # Лот НЕ отключаем: мы не знаем, какой именно из кандидатов
        # продался, а выключать все подряд — слишком разрушительно.
        return {"status": "manual_hold", "reason": reason}

    if mapping is None or not mapping.enabled:
        reason = (
            f"нет маппинга для funpay_lot_id={event.funpay_lot_id} "
            f"(description={event.description!r})"
            if mapping is None
            else "маппинг выключен (enabled=false)"
        )
        log.error(reason)
        lot_to_disable = (
            mapping.funpay_lot_id if mapping is not None else event.funpay_lot_id
        )
        async with session_factory()() as session:
            order = existing or await create_order(
                session,
                funpay_order_id=event.funpay_order_id,
                funpay_lot_id=event.funpay_lot_id or 0,
                ns_service_id=0,
                buyer_username=event.buyer_username,
                buyer_user_id=event.buyer_user_id,
                chat_id=event.chat_id,
                quantity=event.quantity,
                funpay_price_rub=event.funpay_price_rub,
                description=event.description,
            )
            await update_order(session, order, status="failed", error=reason)
            await session.commit()
        if telegram is not None:
            await telegram.order_failure(
                funpay_order_id=event.funpay_order_id, reason=reason
            )
        await _emergency_disable_lot(
            lot_to_disable,
            funpay_client,
            telegram,
            reason=reason,
            log=log,
        )
        return {"status": "failed", "reason": reason}

    # ─── 4. Создаём/находим Order в БД ───
    effective_funpay_lot_id = (
        event.funpay_lot_id if event.funpay_lot_id > 0 else mapping.funpay_lot_id
    )
    async with session_factory()() as session:
        db_order = existing or await create_order(
            session,
            funpay_order_id=event.funpay_order_id,
            funpay_lot_id=effective_funpay_lot_id,
            ns_service_id=mapping.ns_service_id,
            buyer_username=event.buyer_username,
            buyer_user_id=event.buyer_user_id,
            chat_id=event.chat_id,
            quantity=event.quantity,
            funpay_price_rub=event.funpay_price_rub,
            description=event.description,
        )
        await session.commit()
        db_order_id = db_order.id
        order_status = db_order.status
        existing_ns_custom_id = db_order.ns_custom_id
        existing_ns_price_usd = db_order.ns_price_usd
        existing_age_seconds = _order_age_seconds(db_order)

    if not force_delivery and _is_hard_timeout(db_order, settings):
        return await _trigger_manual_hold(
            funpay_order_id=event.funpay_order_id,
            stage="before_ns_purchase",
            reason=(
                f"hard timeout: заказу {int(existing_age_seconds)}s, "
                f"лимит {settings.order_delivery_hard_timeout_seconds}s; "
                "автопокупка остановлена"
            ),
            funpay_client=funpay_client,
            telegram=telegram,
            log=log,
        )

    # Если продавец уже вмешался вручную после оплаты, не покупаем код в NS:
    # это дешевле и безопаснее, чем купить pins и остановиться только перед доставкой.
    if not force_delivery and order_status == "received" and existing_ns_custom_id is None:
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
            if await _should_hold_delivery(
                session,
                db_order,
                grace_seconds=int(settings.chat_help_auto_delivery_grace_seconds),
                manual_guard_seconds=int(settings.order_manual_intervention_guard_seconds),
            ):
                await update_order(
                    session,
                    db_order,
                    status="manual_hold",
                    error=(
                        "manual_hold: help/manual intervention before NS purchase; "
                        "автопокупка остановлена во избежание дубля"
                    ),
                )
                await session.commit()
                if telegram is not None:
                    await telegram.warning(
                        f"🛑 Заказ <code>{event.funpay_order_id}</code> "
                        "остановлен до покупки в NS: в чате было ручное вмешательство."
                    )
                return {
                    "status": "manual_hold",
                    "reason": "manual intervention before ns purchase",
                }

    # ─── 5. Приветствие в чате FunPay (один раз, при первом заходе) ───
    if (
        existing is None
        and funpay_client is not None
        and event.chat_id is not None
        and not dry_run
    ):
        try:
            await funpay_client.send_message(
                event.chat_id,
                templates.order_received(event.buyer_username or "друг"),
            )
        except Exception as exc:
            log.warning(f"Не отправил приветствие в чат: {exc}")

    own_ns = ns_client is None
    if own_ns:
        ns_client = NSClient()
        await ns_client.__aenter__()

    try:
        purchase_outcome = await run_purchase(
            event,
            mapping,
            ns_client=ns_client,
            funpay_client=funpay_client,
            telegram=telegram,
            settings=settings,
            dry_run=dry_run,
            log=log,
            db_order_id=db_order_id,
            order_status=order_status,
            ns_custom_id=existing_ns_custom_id,
            ns_price_usd=existing_ns_price_usd,
            effective_funpay_lot_id=effective_funpay_lot_id,
        )
        # dict — терминальный исход (failed/dry_run/manual_hold);
        # tuple — успех (pins получены).
        if not isinstance(purchase_outcome, tuple):
            return purchase_outcome
        pins, ns_custom_id, ns_price_usd = purchase_outcome

        # Сохраняем pins И помечаем pins_ready — это критическая точка:
        # дальше деньги уже не вернуть, надо обязательно доставить.
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
            # Hard-timeout сразу после получения pins: pins на руках,
            # но мы переползли общий лимит. Сначала сохраняем pins
            # отдельным flush'ом, чтобы они не потерялись, а статус и
            # alert ставит _trigger_manual_hold ниже.
            if not force_delivery and _is_hard_timeout(db_order, settings):
                age_now = int(_order_age_seconds(db_order))
                await update_order(session, db_order, pins=pins)
                await session.commit()
                return await _trigger_manual_hold(
                    funpay_order_id=event.funpay_order_id,
                    stage="post_pins_pre_delivery",
                    reason=(
                        f"hard timeout с pins на руках: age={age_now}s, "
                        f"лимит={settings.order_delivery_hard_timeout_seconds}s; "
                        f"pins сохранены, нажми Retry для доставки"
                    ),
                    funpay_client=funpay_client,
                    telegram=telegram,
                    log=log,
                )
            if (
                not force_delivery
                and await _should_hold_delivery(
                    session,
                    db_order,
                    grace_seconds=int(settings.chat_help_auto_delivery_grace_seconds),
                    manual_guard_seconds=int(
                        settings.order_manual_intervention_guard_seconds
                    ),
                )
            ):
                await update_order(
                    session,
                    db_order,
                    pins=pins,
                    status="manual_hold",
                    error=(
                        "manual_hold: help/manual intervention before delivery; "
                        "pins сохранены, автоматическая выдача остановлена во избежание дубля"
                    ),
                )
                await session.commit()
                if telegram is not None:
                    await telegram.warning(
                        f"🛑 Заказ <code>{event.funpay_order_id}</code> "
                        "поставлен на ручную проверку после help/manual intervention. "
                        "Pins сохранены, покупателю автоматически не отправлены."
                    )
                return {
                    "status": "manual_hold",
                    "ns_custom_id": ns_custom_id,
                    "pins_count": len(pins),
                    "reason": "help/manual intervention before delivery",
                }
            await update_order(session, db_order, status="pins_ready", pins=pins)
            await session.commit()
        log.info(f"NS pins получены ({len(pins)} шт), статус pins_ready")

        # ─── 9. Доставка клиенту ───
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
        assert db_order is not None
        return await _deliver_pins(
            event, db_order, pins, funpay_client, telegram, log,
            ns_custom_id=ns_custom_id, ns_price_usd=ns_price_usd,
            force_delivery=force_delivery,
            help_grace_seconds=int(settings.chat_help_auto_delivery_grace_seconds),
        )

    finally:
        if own_ns and ns_client is not None:
            await ns_client.__aexit__(None, None, None)
