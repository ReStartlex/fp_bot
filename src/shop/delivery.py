"""
Sprint 5 — auto-delivery shop-заказов через NS.

После того как покупатель оплатил заказ из баланса (ShopOrder.status='paid'),
этот модуль доставляет товар:
   paid → delivering (резервируем NS custom_id) → delivered (с pins)
              │
              └─→ failed → (refund баланса покупателю отдельной функцией)

Алгоритм одного цикла:
  1. order_info(custom_id) — может быть уже создан в NS если retry;
  2. иначе create_order(service_id, fields, custom_id);
  3. pay_order(custom_id) — списывает с NS-баланса оператора;
  4. wait_order_completion(custom_id) — поллинг до финального статуса;
  5. если COMPLETED + pins → mark_delivered + notify buyer;
  6. если REFUNDED/CANCELLED → mark_failed → refund balance + notify;
  7. при ошибке NS — mark_failed с описанием.

Идемпотентность:
  * ns_custom_id (UUID4) кладётся в БД ДО первого вызова NS — на повторе
    мы попадаем на тот же NS-заказ;
  * order_info pre-check защищает от двойного create;
  * pay_order — NS гарантирует что повторный вызов на уже оплаченный
    заказ не списывает повторно.

Запускается APScheduler-job'ом каждые N секунд, или вручную после
успешного checkout (мы делаем оба).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger

from src.config import Settings
from src.config_runtime import get_shop_referral_percent
from src.db.session import session_factory
from src.ns.client import NSClient
from src.ns.exceptions import (
    NSError,
    NSInsufficientFunds,
    NSOrderTimeoutError,
)
from src.ns.models import OrderStatus
from src.shop.repo import (
    SHOP_ORDER_STATUS_DELIVERING,
    SHOP_ORDER_STATUS_PAID,
    credit_referral_cashback,
    get_shop_order,
    list_orders_awaiting_delivery,
    mark_order_delivered,
    mark_order_delivering,
    mark_order_failed,
    refund_failed_order,
)


# Callback signatures для уведомлений
NotifyBuyerFn = Callable[[int, str], Awaitable[None]]
NotifyOwnerFn = Callable[[str], Awaitable[None]]


@dataclass
class DeliveryOutcome:
    """Финальный результат одной попытки доставки."""
    order_id: int
    delivered: bool = False
    failed: bool = False
    pending: bool = False        # ещё не финализирован, retry на следующем тике
    error: str | None = None
    pins: list | None = None
    cashback_credited_kopecks: int = 0


def _is_valid_uuid4(s: str | None) -> bool:
    """Проверка UUID4 (NS требует строго UUID4)."""
    if not s:
        return False
    try:
        u = uuid.UUID(s, version=4)
    except (ValueError, AttributeError):
        return False
    return u.version == 4 and str(u) == s.lower()


def _new_custom_id(order_id: int) -> str:
    """
    Генерирует UUID4 для NS custom_id. order_id не закладываем в строку —
    NS требует именно UUID4, и наша связка хранится в БД (shop_orders.ns_custom_id).
    """
    return str(uuid.uuid4())


def _build_ns_fields(fields_json_raw: str) -> list[dict]:
    """
    Парсит ShopOrder.fields_json. На Sprint 5 поддерживаем только
    пустой список — UI ввода полей не реализован (см. checkout.py).

    Если в БД невалидный JSON — возвращаем [] (NS примет если поля
    не обязательны для этого service_id, иначе вернёт ошибку и заказ
    закроется как failed).
    """
    if not fields_json_raw:
        return []
    try:
        parsed = json.loads(fields_json_raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, list):
        return parsed
    return []


async def _ensure_ns_custom_id(order_id: int) -> str:
    """
    Idempotently выставляет ns_custom_id и переводит заказ в DELIVERING.

    Это делается atomically per-order, перед любым обращением к NS,
    чтобы при retry на следующем тике мы взяли ТОТ ЖЕ custom_id и
    обратились к тому же NS-заказу.
    """
    async with session_factory()() as session:
        order = await get_shop_order(session, order_id)
        if order is None:
            raise RuntimeError(f"order {order_id} not found")
        if order.ns_custom_id and _is_valid_uuid4(order.ns_custom_id):
            # Уже резервировали ранее
            if order.status == SHOP_ORDER_STATUS_PAID:
                # ID есть, но статус не сдвинут — поправим
                await mark_order_delivering(
                    session, order_id=order_id,
                    ns_custom_id=order.ns_custom_id,
                )
                await session.commit()
            return order.ns_custom_id
        # Новый UUID4
        custom_id = _new_custom_id(order_id)
        await mark_order_delivering(
            session, order_id=order_id, ns_custom_id=custom_id,
        )
        await session.commit()
        return custom_id


async def deliver_shop_order_once(
    order_id: int,
    *,
    ns: NSClient,
    settings: Settings,
    notify_buyer: NotifyBuyerFn | None = None,
    notify_owner: NotifyOwnerFn | None = None,
) -> DeliveryOutcome:
    """
    Один атомарный цикл доставки одного заказа.

    Никогда не бросает наружу — все ошибки логируются и попадают в
    outcome.error / outcome.failed.

    Что делает:
      1. Гарантирует ns_custom_id (UUID4) и переводит в DELIVERING.
      2. Пытается получить order_info — если NS-заказ уже COMPLETED,
         используем pins оттуда.
      3. Иначе create_order + pay_order + wait_order_completion.
      4. Финализирует ShopOrder + начисляет cashback или делает refund.
      5. Уведомляет покупателя и оператора.
    """
    async with session_factory()() as session:
        order = await get_shop_order(session, order_id)
    if order is None:
        return DeliveryOutcome(order_id=order_id, failed=True, error="not found")
    if order.status not in (SHOP_ORDER_STATUS_PAID, SHOP_ORDER_STATUS_DELIVERING):
        # Уже delivered / failed / refunded — ничего делать не нужно.
        return DeliveryOutcome(order_id=order_id, pending=False)

    log = logger.bind(shop_order_id=order_id)

    # 1. Резервируем NS custom_id
    try:
        custom_id = await _ensure_ns_custom_id(order_id)
    except Exception as exc:
        log.opt(exception=exc).error(f"_ensure_ns_custom_id упал: {exc}")
        return DeliveryOutcome(order_id=order_id, failed=True, error=str(exc))

    # 2. Pre-check: возможно уже создан / оплачен в NS
    pre_info = None
    try:
        pre_info = await ns.order_info(custom_id)
    except NSError as exc:
        # 404 / no_order — нормально, продолжаем create
        log.debug(f"NS order_info pre-check: {exc} (продолжаем create)")
    except Exception as exc:
        log.warning(f"NS order_info pre-check упал: {exc}")

    pins: list | None = None
    if pre_info is not None:
        status_enum = getattr(pre_info, "status_enum", None)
        if status_enum == OrderStatus.COMPLETED and pre_info.pins:
            pins = list(pre_info.pins)
            log.info(f"NS idempotency: pins уже выданы ({len(pins)} шт)")
        elif status_enum in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
            error_text = (
                f"NS уже отменил заказ: {pre_info.status_message or status_enum}"
            )
            return await _finalize_failure(
                order_id, error_text, settings,
                notify_buyer=notify_buyer, notify_owner=notify_owner, log=log,
            )

    # 3. Create + pay (если ещё не оплачен)
    if pins is None:
        # Create_order
        if pre_info is None:
            ns_fields = _build_ns_fields(order.fields_json)
            try:
                await ns.create_order(
                    service_id=order.ns_service_id,
                    fields=ns_fields,
                    custom_id=custom_id,
                )
            except NSError as exc:
                error_text = f"NS create_order упал: {exc}"
                log.error(error_text)
                return await _finalize_failure(
                    order_id, error_text, settings,
                    notify_buyer=notify_buyer, notify_owner=notify_owner, log=log,
                )

        # Pay_order — снимает $ с NS-баланса оператора
        try:
            pay_resp = await ns.pay_order(custom_id)
        except NSInsufficientFunds as exc:
            error_text = (
                f"NS отказал: недостаточно средств у оператора "
                f"(balance={exc.balance})"
            )
            log.error(error_text)
            return await _finalize_failure(
                order_id, error_text, settings,
                notify_buyer=notify_buyer, notify_owner=notify_owner, log=log,
            )
        except NSError as exc:
            # Если уже оплачен (повторный вызов на already-paid) — это ОК,
            # NS вернёт информативную ошибку, продолжаем с wait_completion.
            log.warning(f"NS pay_order вернул {exc} (возможно дубль, продолжаем)")
        else:
            if pay_resp.pins:
                pins = list(pay_resp.pins)
                log.info(f"NS pay_order: pins сразу ({len(pins)} шт)")

    # 4. Wait completion (если pins ещё не получены)
    if pins is None:
        wait_timeout = float(getattr(settings, "ns_order_timeout_seconds", 600))
        try:
            info = await ns.wait_order_completion(
                custom_id, timeout_seconds=min(wait_timeout, 60.0),
            )
        except NSOrderTimeoutError as exc:
            # Деньги в NS уже сняты, но pins не пришли вовремя.
            # На следующем тике reaper попробует ещё раз — мы НЕ помечаем
            # failed сейчас, оставляем DELIVERING.
            log.warning(
                f"NS wait timeout — оставляем в delivering для retry: {exc}"
            )
            return DeliveryOutcome(order_id=order_id, pending=True)
        except Exception as exc:
            error_text = f"NS wait упал: {exc}"
            log.exception(error_text)
            return await _finalize_failure(
                order_id, error_text, settings,
                notify_buyer=notify_buyer, notify_owner=notify_owner, log=log,
            )
        status_enum = getattr(info, "status_enum", None)
        if status_enum == OrderStatus.COMPLETED:
            pins = list(info.pins or [])
        elif status_enum in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
            return await _finalize_failure(
                order_id,
                f"NS вернул возврат/отмену: {info.status_message or status_enum}",
                settings,
                notify_buyer=notify_buyer, notify_owner=notify_owner, log=log,
            )

    if not pins:
        # Уши странного state — completed но пинов нет.
        error_text = "NS completed но pins пусты"
        log.error(error_text)
        return await _finalize_failure(
            order_id, error_text, settings,
            notify_buyer=notify_buyer, notify_owner=notify_owner, log=log,
        )

    # 5. Финал: mark_delivered + cashback + notify
    async with session_factory()() as session:
        await mark_order_delivered(
            session, order_id=order_id,
            pins_json=json.dumps(pins, ensure_ascii=False),
        )
        await session.commit()

    cashback_credited = 0
    try:
        cashback_percent = await get_shop_referral_percent(settings)
        async with session_factory()() as session:
            cashback_credited = await credit_referral_cashback(
                session, order_id=order_id,
                cashback_percent=cashback_percent,
            )
            await session.commit()
        if cashback_credited > 0:
            log.info(f"referral cashback: +{cashback_credited}коп инвайтеру")
    except Exception as exc:
        # Cashback не должен валить delivery — деньги покупателю важнее.
        # Залогируем, на следующем deliver попытаемся снова (idempotency
        # гарантирует что начислим ровно один раз).
        log.warning(f"cashback hook упал: {exc}")

    # Уведомление покупателю с pins
    async with session_factory()() as session:
        order_final = await get_shop_order(session, order_id)
    if notify_buyer is not None and order_final is not None:
        try:
            await notify_buyer(
                order_final.user_id,
                _format_pins_message(order_final, pins),
            )
        except Exception as exc:
            log.warning(f"notify_buyer упал: {exc}")

    log.success(f"shop order {order_id} DELIVERED ({len(pins)} pins)")
    return DeliveryOutcome(
        order_id=order_id, delivered=True, pins=pins,
        cashback_credited_kopecks=cashback_credited,
    )


def _format_pins_message(order, pins: list) -> str:
    """
    Сообщение покупателю с pins/contents.

    NS возвращает pins как список dict'ов с разной структурой в зависимости
    от типа товара. Стандартные ключи: 'pin', 'serial', 'code', 'content'.
    Формат каждой строки: <b>pin</b> · serial (если есть).
    """
    lines = [f"✅ <b>Заказ #{order.id} выполнен!</b>"]
    lines.append(f"🛒 {order.ns_service_name}")
    lines.append("")
    lines.append("🔑 <b>Твои коды:</b>")
    for i, p in enumerate(pins, start=1):
        if isinstance(p, dict):
            code = p.get("pin") or p.get("code") or p.get("content") or "?"
            serial = p.get("serial")
            if serial:
                lines.append(f"  {i}. <code>{code}</code> · serial: <code>{serial}</code>")
            else:
                lines.append(f"  {i}. <code>{code}</code>")
        else:
            lines.append(f"  {i}. <code>{p}</code>")
    lines.append("")
    lines.append(
        "<i>Спасибо за покупку в NeuroDrop! Если что-то не активируется — "
        "обратись в 🆘 Поддержку, поможем.</i>"
    )
    return "\n".join(lines)


async def _finalize_failure(
    order_id: int,
    error: str,
    settings: Settings,
    *,
    notify_buyer: NotifyBuyerFn | None,
    notify_owner: NotifyOwnerFn | None,
    log,
) -> DeliveryOutcome:
    """
    Финализирует неудачный заказ:
      * mark_failed (error string в БД)
      * refund balance → ledger entry с reason="refund"
      * notify покупателя (refund на баланс) и оператора (диагностика)

    Безопасно вызывать многократно — refund идемпотентен.
    """
    async with session_factory()() as session:
        await mark_order_failed(session, order_id=order_id, error=error)
        await session.commit()

    try:
        async with session_factory()() as session:
            await refund_failed_order(session, order_id=order_id)
            await session.commit()
    except Exception as exc:
        log.opt(exception=exc).error(f"refund упал: {exc}")

    async with session_factory()() as session:
        order = await get_shop_order(session, order_id)

    if notify_buyer is not None and order is not None:
        try:
            await notify_buyer(
                order.user_id,
                "❌ <b>Заказ не выполнен</b>\n\n"
                f"#<code>{order.id}</code> · {order.ns_service_name}\n\n"
                f"<i>Причина: {error[:200]}</i>\n\n"
                f"💰 Средства возвращены на баланс. "
                "Можешь попробовать заказать снова или выбрать другой "
                "номинал/регион.",
            )
        except Exception as exc:
            log.warning(f"notify_buyer (fail) упал: {exc}")

    if notify_owner is not None and order is not None:
        try:
            await notify_owner(
                f"⚠ <b>Shop order #{order.id} failed</b>\n"
                f"user={order.user_id} · service={order.ns_service_name}\n"
                f"refund={order.balance_used_kopecks // 100}₽ "
                f"({order.balance_used_kopecks} коп)\n"
                f"error: <code>{error[:300]}</code>"
            )
        except Exception as exc:
            log.warning(f"notify_owner (fail) упал: {exc}")

    return DeliveryOutcome(
        order_id=order_id, failed=True, error=error,
    )


async def poll_shop_deliveries_once(
    *,
    ns: NSClient,
    settings: Settings,
    notify_buyer: NotifyBuyerFn | None = None,
    notify_owner: NotifyOwnerFn | None = None,
    max_per_run: int = 5,
) -> dict[str, int]:
    """
    Один прогон фонового воркера: берёт N awaiting-заказов и пытается
    доставить каждый.

    Возвращает метрики прогона:
      {"checked": N, "delivered": X, "failed": Y, "pending": Z}

    Используется в APScheduler job'е (см. src/main.py) и опционально
    в тестах для деривации полного flow.
    """
    async with session_factory()() as session:
        orders = await list_orders_awaiting_delivery(session, limit=max_per_run)

    if not orders:
        return {"checked": 0, "delivered": 0, "failed": 0, "pending": 0}

    delivered = 0
    failed = 0
    pending = 0
    for order in orders:
        outcome = await deliver_shop_order_once(
            order.id, ns=ns, settings=settings,
            notify_buyer=notify_buyer, notify_owner=notify_owner,
        )
        if outcome.delivered:
            delivered += 1
        elif outcome.failed:
            failed += 1
        else:
            pending += 1
        # Между заказами — короткая пауза, чтобы не молотить NS API
        await asyncio.sleep(0.5)

    logger.info(
        f"shop delivery: checked={len(orders)} "
        f"delivered={delivered} failed={failed} pending={pending}"
    )
    return {
        "checked": len(orders),
        "delivered": delivered,
        "failed": failed,
        "pending": pending,
    }
