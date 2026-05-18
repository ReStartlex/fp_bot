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
import json
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy import select

from src.alerts.telegram import TelegramNotifier
from src.chat import templates
from src.config import Settings, get_settings
from src.db.models import Mapping, Order
from src.db.repo import (
    create_order,
    find_order_by_funpay_id,
    update_order,
)
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.ns import NSClient
from src.ns.exceptions import (
    NSError,
    NSInsufficientFunds,
    NSOrderTimeoutError,
)
from src.ns.models import OrderStatus


# Per-key мьютекс: гарантирует, что одновременно над одним заказом
# работает только одна корутина (на этот процесс).
_order_locks: dict[str, asyncio.Lock] = {}


def _lock_for(funpay_order_id: str) -> asyncio.Lock:
    lock = _order_locks.get(funpay_order_id)
    if lock is None:
        lock = asyncio.Lock()
        _order_locks[funpay_order_id] = lock
    return lock


@dataclass
class FunPayOrderEvent:
    """Нормализованные данные FunPay-заказа, которые нам нужны."""
    funpay_order_id: str
    funpay_lot_id: int
    buyer_username: Optional[str]
    buyer_user_id: Optional[int]
    chat_id: Optional[int]
    quantity: int = 1
    funpay_price_rub: Optional[float] = None


async def _find_mapping(funpay_lot_id: int) -> Mapping | None:
    async with session_factory()() as session:
        result = await session.execute(
            select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
        )
        return result.scalar_one_or_none()


def _build_ns_fields(template_json: str | None, quantity: int) -> list[dict]:
    """
    Превратить ns_fields_template из mapping в готовый список fields
    для NS create_order. Поддерживается подстановка `@QUANTITY`.
    """
    if not template_json:
        return [{"key": "quantity", "value": quantity}]
    try:
        parsed = json.loads(template_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ns_fields_template не валидный JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("ns_fields_template должен быть JSON-объектом")
    fields: list[dict] = []
    for key, value in parsed.items():
        if isinstance(value, str) and value.strip() == "@QUANTITY":
            value = quantity
        fields.append({"key": key, "value": value})
    return fields


def _pins_from_order(order: Order) -> list:
    """Прочитать список пинов из Order.pins_json."""
    if not order.pins_json:
        return []
    try:
        data = json.loads(order.pins_json)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


async def process_funpay_order(
    event: FunPayOrderEvent,
    *,
    settings: Settings | None = None,
    ns_client: NSClient | None = None,
    funpay_client: FunPayClient | None = None,
    telegram: TelegramNotifier | None = None,
    dry_run: bool | None = None,
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
            event, settings, ns_client, funpay_client, telegram, dry_run, log
        )


async def _process_locked(
    event: FunPayOrderEvent,
    settings: Settings,
    ns_client: NSClient | None,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    dry_run: bool,
    log,
) -> dict:
    log.info(
        f"Обработка FunPay-заказа: lot={event.funpay_lot_id}, "
        f"qty={event.quantity}, buyer={event.buyer_username}, "
        f"dry_run={dry_run}"
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

    # ─── 2. Доставка только-pins (без обращения к NS) ───
    # Случай: pins_ready — деньги списаны, коды есть, но send_message
    # клиенту в прошлый раз упал. Просто повторяем доставку.
    if existing is not None and existing.status == "pins_ready":
        pins = _pins_from_order(existing)
        if pins:
            log.warning(
                f"Повторная доставка pins_ready: {len(pins)} код(а/ов)"
            )
            return await _deliver_pins(
                event, existing, pins, funpay_client, telegram, log
            )
        log.error("pins_ready без pins_json — пометить failed")
        await _mark_failed(
            existing.id, "pins_ready без сохранённых pins",
            telegram, event,
        )
        return {"status": "failed", "reason": "pins_ready без pins"}

    # ─── 3. Маппинг ───
    mapping = await _find_mapping(event.funpay_lot_id)
    if mapping is None or not mapping.enabled:
        reason = (
            f"нет маппинга для funpay_lot_id={event.funpay_lot_id}"
            if mapping is None
            else "маппинг выключен (enabled=false)"
        )
        log.error(reason)
        async with session_factory()() as session:
            order = existing or await create_order(
                session,
                funpay_order_id=event.funpay_order_id,
                funpay_lot_id=event.funpay_lot_id,
                ns_service_id=0,
                buyer_username=event.buyer_username,
                buyer_user_id=event.buyer_user_id,
                chat_id=event.chat_id,
                quantity=event.quantity,
                funpay_price_rub=event.funpay_price_rub,
            )
            await update_order(session, order, status="failed", error=reason)
            await session.commit()
        if telegram is not None:
            await telegram.order_failure(
                funpay_order_id=event.funpay_order_id, reason=reason
            )
        return {"status": "failed", "reason": reason}

    # ─── 4. Создаём/находим Order в БД ───
    async with session_factory()() as session:
        db_order = existing or await create_order(
            session,
            funpay_order_id=event.funpay_order_id,
            funpay_lot_id=event.funpay_lot_id,
            ns_service_id=mapping.ns_service_id,
            buyer_username=event.buyer_username,
            buyer_user_id=event.buyer_user_id,
            chat_id=event.chat_id,
            quantity=event.quantity,
            funpay_price_rub=event.funpay_price_rub,
        )
        await session.commit()
        db_order_id = db_order.id
        order_status = db_order.status
        existing_ns_custom_id = db_order.ns_custom_id
        existing_ns_price_usd = db_order.ns_price_usd

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
        ns_custom_id = existing_ns_custom_id
        ns_price_usd = existing_ns_price_usd

        # ─── 6. NS create_order (если ещё не создан) ───
        if order_status in ("received",) or ns_custom_id is None:
            try:
                ns_fields = _build_ns_fields(
                    mapping.ns_fields_template, event.quantity
                )
            except ValueError as exc:
                error_text = f"Ошибка в шаблоне ns_fields_template: {exc}"
                log.error(error_text)
                await _mark_failed(db_order_id, error_text, telegram, event)
                return {"status": "failed", "reason": error_text}

            try:
                created = await ns_client.create_order(
                    service_id=mapping.ns_service_id, fields=ns_fields
                )
            except NSError as exc:
                error_text = f"NS create_order упал: {exc}"
                log.error(error_text)
                await _mark_failed(db_order_id, error_text, telegram, event)
                return {"status": "failed", "reason": error_text}

            ns_custom_id = created.custom_id
            ns_price_usd = float(created.total_to_pay)
            log.info(
                f"NS create_order: custom_id={ns_custom_id}, "
                f"к оплате={ns_price_usd:.4f} USD"
            )
            async with session_factory()() as session:
                db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
                assert db_order is not None
                await update_order(
                    session, db_order,
                    status="ns_created",
                    ns_custom_id=ns_custom_id,
                    ns_price_usd=ns_price_usd,
                )
                await session.commit()
            order_status = "ns_created"

        if dry_run:
            log.warning(
                f"DRY-RUN: ENABLE_REAL_ACTIONS=false → НЕ оплачиваю NS-заказ "
                f"({ns_custom_id}). NS сам отменит его через ~10 минут."
            )
            return {
                "status": "ns_created",
                "ns_custom_id": ns_custom_id,
                "ns_price_usd": ns_price_usd,
                "dry_run": True,
            }

        # ─── 7. NS pay_order (если ещё не оплачен) ───
        pins: list = []
        if order_status == "ns_created":
            try:
                pay_resp = await ns_client.pay_order(ns_custom_id)
            except NSInsufficientFunds as exc:
                error_text = f"Недостаточно средств на NS: balance={exc.balance}"
                log.error(error_text)
                await _mark_failed(db_order_id, error_text, telegram, event)
                return {"status": "failed", "reason": error_text}
            except NSError as exc:
                error_text = f"NS pay_order упал: {exc}"
                log.error(error_text)
                await _mark_failed(db_order_id, error_text, telegram, event)
                return {"status": "failed", "reason": error_text}
            log.info(f"NS pay_order: status={pay_resp.status}")
            pins = list(pay_resp.pins or [])
            async with session_factory()() as session:
                db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
                assert db_order is not None
                await update_order(session, db_order, status="ns_paid")
                await session.commit()
            order_status = "ns_paid"

        # ─── 8. Получаем pins (если ещё не получили) ───
        if order_status == "ns_paid" and not pins:
            try:
                info = await ns_client.wait_order_completion(ns_custom_id)
            except NSOrderTimeoutError as exc:
                error_text = f"NS заказ не завершился по тайм-ауту: {exc}"
                log.error(error_text)
                await _mark_failed(db_order_id, error_text, telegram, event)
                return {"status": "failed", "reason": error_text}
            if info.status_enum == OrderStatus.COMPLETED and info.pins:
                pins = list(info.pins)
            elif info.status_enum in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
                error_text = f"NS вернул возврат/отмену: {info.status_message}"
                log.error(error_text)
                await _mark_failed(db_order_id, error_text, telegram, event)
                return {"status": "failed", "reason": error_text}

        if not pins:
            error_text = "NS заказ завершился, но pins пустой"
            log.error(error_text)
            await _mark_failed(db_order_id, error_text, telegram, event)
            return {"status": "failed", "reason": error_text}

        # Сохраняем pins И помечаем pins_ready — это критическая точка:
        # дальше деньги уже не вернуть, надо обязательно доставить.
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
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
        )

    finally:
        if own_ns and ns_client is not None:
            await ns_client.__aexit__(None, None, None)


async def _deliver_pins(
    event: FunPayOrderEvent,
    db_order: Order,
    pins: list,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    log,
    *,
    ns_custom_id: str | None = None,
    ns_price_usd: float | None = None,
) -> dict:
    """
    Шаг доставки: отправляет коды в чат FunPay, обновляет статус.
    На вход подаются уже сохранённые pins. Если FunPay упал — статус
    останется pins_ready, повторим в следующий вход.
    """
    ns_custom_id = ns_custom_id or db_order.ns_custom_id
    ns_price_usd = ns_price_usd if ns_price_usd is not None else db_order.ns_price_usd

    delivery_text = templates.delivery(event.buyer_username or "друг", pins)

    if funpay_client is None or event.chat_id is None:
        log.warning(
            "FunPay-клиент или chat_id отсутствуют — доставка отложена; "
            "статус остаётся pins_ready"
        )
        return {
            "status": "pins_ready",
            "ns_custom_id": ns_custom_id,
            "pins_count": len(pins),
            "reason": "no funpay client or chat_id",
        }

    try:
        await funpay_client.send_message(event.chat_id, delivery_text)
    except Exception as exc:
        log.error(f"Доставка в чат FunPay упала: {exc}; статус остаётся pins_ready")
        if telegram is not None:
            await telegram.error(
                f"⚠ Не доставил pins в чат FunPay (order "
                f"<code>{event.funpay_order_id}</code>): <code>{exc}</code>. "
                f"Статус: pins_ready. Бот повторит доставку при следующей "
                f"возможности."
            )
        return {
            "status": "pins_ready",
            "ns_custom_id": ns_custom_id,
            "pins_count": len(pins),
            "delivery_error": str(exc),
        }

    log.success(f"Доставил {len(pins)} код(а/ов) в чат {event.chat_id}")
    async with session_factory()() as session:
        order = await find_order_by_funpay_id(session, event.funpay_order_id)
        assert order is not None
        await update_order(session, order, status="delivered")
        await session.commit()

    if telegram is not None:
        await telegram.order_success(
            funpay_order_id=event.funpay_order_id,
            ns_custom_id=ns_custom_id,
            ns_price_usd=ns_price_usd,
            funpay_price_rub=event.funpay_price_rub,
            buyer_username=event.buyer_username,
        )

    return {
        "status": "delivered",
        "ns_custom_id": ns_custom_id,
        "ns_price_usd": ns_price_usd,
        "pins_count": len(pins),
    }


async def _mark_failed(
    db_order_id: int | None,
    reason: str,
    telegram: TelegramNotifier | None,
    event: FunPayOrderEvent,
) -> None:
    if db_order_id is not None:
        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, event.funpay_order_id)
            if order is not None:
                await update_order(session, order, status="failed", error=reason)
                await session.commit()
    if telegram is not None:
        await telegram.order_failure(
            funpay_order_id=event.funpay_order_id, reason=reason
        )
