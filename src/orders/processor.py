"""
Pipeline обработки заказа: FunPay-событие → NS-покупка → доставка кодов.

Этот модуль НЕ слушает FunPay-события напрямую (это будет делать watcher в F4-F5).
Здесь — функция, в которую передают данные FunPay-заказа, и она:

1. Записывает заказ в БД (идемпотентно по `funpay_order_id`).
2. Находит mapping (funpay_lot_id -> ns_service_id).
3. Создаёт заказ на NS (`create_order`).
4. Оплачивает на NS (`pay_order`) — только если `ENABLE_REAL_ACTIONS=true`.
5. Дожидается завершения, забирает пины.
6. Отправляет шаблон + коды в чат FunPay.
7. Шлёт алерты в Telegram.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy import select

from src.alerts.telegram import TelegramNotifier
from src.chat import templates
from src.config import Settings, get_settings
from src.db.models import Mapping
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
    Превратить ns_fields_template из mapping в готовый список fields для NS create_order.

    Поддерживается подстановка `@QUANTITY`. Если шаблон не задан — отправляется
    минимальный набор {"quantity": <quantity>}.
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
    Основной pipeline. Возвращает dict со статусом и деталями для логов/тестов.

    Безопасно вызывать многократно с тем же `funpay_order_id`: если заказ уже
    в БД и не в провальном статусе — повторно ничего не делаем.
    """
    settings = settings or get_settings()
    if dry_run is None:
        dry_run = not settings.enable_real_actions

    log = logger.bind(funpay_order_id=event.funpay_order_id)
    log.info(
        f"Начинаю обработку FunPay-заказа: lot={event.funpay_lot_id}, "
        f"qty={event.quantity}, buyer={event.buyer_username}, dry_run={dry_run}"
    )

    # 0. Идемпотентность
    async with session_factory()() as session:
        existing = await find_order_by_funpay_id(session, event.funpay_order_id)
        if existing is not None and existing.status in {"delivered", "ns_paid"}:
            log.info(f"Заказ уже обработан (status={existing.status}), пропускаю")
            return {
                "status": existing.status,
                "skipped": True,
                "ns_custom_id": existing.ns_custom_id,
            }

    # 1. Маппинг
    mapping = await _find_mapping(event.funpay_lot_id)
    if mapping is None or not mapping.enabled:
        reason = (
            f"нет маппинга для funpay_lot_id={event.funpay_lot_id}"
            if mapping is None
            else "маппинг выключен (enabled=false)"
        )
        log.error(reason)
        async with session_factory()() as session:
            db_order = await create_order(
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
            await update_order(session, db_order, status="failed", error=reason)
            await session.commit()
        if telegram is not None:
            await telegram.order_failure(
                funpay_order_id=event.funpay_order_id, reason=reason
            )
        return {"status": "failed", "reason": reason}

    # 2. Сохраняем заказ в БД
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

    # 3. Сразу отвечаем покупателю что заказ принят
    if funpay_client is not None and event.chat_id is not None and not dry_run:
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
        # 4. NS create_order
        try:
            ns_fields = _build_ns_fields(mapping.ns_fields_template, event.quantity)
        except ValueError as exc:
            error_text = f"Ошибка в шаблоне ns_fields_template маппинга: {exc}"
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
            f"NS-заказ создан: custom_id={ns_custom_id}, "
            f"к оплате={ns_price_usd:.4f} USD"
        )

        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
            await update_order(
                session,
                db_order,
                status="ns_created",
                ns_custom_id=ns_custom_id,
                ns_price_usd=ns_price_usd,
            )
            await session.commit()

        if dry_run:
            log.warning(
                "DRY-RUN: ENABLE_REAL_ACTIONS=false → НЕ оплачиваю NS-заказ "
                f"({ns_custom_id}). NS сам отменит его через ~10 минут."
            )
            return {
                "status": "ns_created",
                "ns_custom_id": ns_custom_id,
                "ns_price_usd": ns_price_usd,
                "dry_run": True,
            }

        # 5. NS pay_order
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

        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
            await update_order(session, db_order, status="ns_paid")
            await session.commit()
        log.info(f"NS pay_order: status={pay_resp.status}")

        # 6. Если pins пришли сразу — выдаём; иначе опрашиваем order_info
        pins = pay_resp.pins or []
        if not pins:
            try:
                info = await ns_client.wait_order_completion(ns_custom_id)
            except NSOrderTimeoutError as exc:
                error_text = f"NS заказ не завершился по тайм-ауту: {exc}"
                log.error(error_text)
                await _mark_failed(db_order_id, error_text, telegram, event)
                return {"status": "failed", "reason": error_text}
            if info.status_enum == OrderStatus.COMPLETED and info.pins:
                pins = info.pins
            elif info.status_enum in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
                error_text = f"NS вернул возврат/отмену: {info.status_message}"
                log.error(error_text)
                await _mark_failed(db_order_id, error_text, telegram, event)
                return {"status": "failed", "reason": error_text}

        # 7. Доставка
        if not pins:
            error_text = "NS заказ завершился, но pins пустой — нечего отдавать"
            log.error(error_text)
            await _mark_failed(db_order_id, error_text, telegram, event)
            return {"status": "failed", "reason": error_text}

        delivery_text = templates.delivery(
            event.buyer_username or "друг", pins
        )
        if funpay_client is not None and event.chat_id is not None:
            try:
                await funpay_client.send_message(event.chat_id, delivery_text)
                log.success(f"Доставил {len(pins)} код(а/ов) в чат {event.chat_id}")
            except Exception as exc:
                log.error(f"Доставка в чат FunPay упала: {exc}")

        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
            await update_order(session, db_order, status="delivered", pins=pins)
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
    finally:
        if own_ns and ns_client is not None:
            await ns_client.__aexit__(None, None, None)


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
