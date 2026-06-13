"""
Стадия purchase: NS create_order -> pay_order -> ожидание pins.
Вынесено из processor.py (P1-1 инкр. 4) без изменения поведения.

run_purchase возвращает:
  * dict  — терминальный исход (failed / dry_run / manual_hold по NS-таймауту):
    processor возвращает его наружу как есть;
  * tuple (pins, ns_custom_id, ns_price_usd) — успех: pins получены, дальше
    processor сохраняет pins_ready и доставляет.

_is_hard_timeout здесь НЕ используется (он остаётся в processor: pre-purchase
и post-pins проверки) — поэтому шов чистый и его patch-таргет не меняется.
"""
from __future__ import annotations

import json
import uuid

from src.config import Settings
from src.db.repo import find_order_by_funpay_id, update_order
from src.db.session import session_factory
from src.ns import NSClient
from src.ns.exceptions import (
    NSError,
    NSInsufficientFunds,
    NSNotFoundError,
    NSOrderTimeoutError,
)
from src.ns.models import OrderInfo, OrderStatus
from src.orders.events import FunPayOrderEvent
from src.orders.stages.common import _order_age_seconds
from src.orders.stages.holds import _mark_failed, _trigger_manual_hold


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


def _is_valid_uuid4(value: str | None) -> bool:
    """
    Истина если value — корректный UUID4-string в каноническом формате.

    NS API проверяет custom_id регуляркой uuid4 (8-4-4-4-12, version=4),
    поэтому нам недостаточно `uuid.UUID(value)` — он принимает и v5/v3/v1.
    UUID версия кодируется в 13-м hex-символе: для v4 это всегда `4`.
    """
    if not value:
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4


async def _ns_check_existing_order(
    ns_client: NSClient,
    custom_id: str,
    log,
) -> OrderInfo | None:
    """Аудит #1: idempotency-проверка существования NS-заказа.

    Возвращает OrderInfo если заказ уже существует в NS, иначе None.
    404 — заказа нет (можно безопасно создавать). Любая другая ошибка
    логируется и трактуется как «не знаем» → None (продолжим create/pay
    как обычно; deterministic custom_id защитит от UUID-дубля при retry).
    """
    try:
        return await ns_client.order_info(custom_id)
    except NSNotFoundError:
        return None
    except NSError as exc:
        log.warning(
            f"NS idempotency check для {custom_id} упал: {exc}; "
            "продолжаю обычным путём"
        )
        return None


async def run_purchase(
    event: FunPayOrderEvent,
    mapping,
    *,
    ns_client: NSClient,
    funpay_client,
    telegram,
    settings: Settings,
    dry_run: bool,
    log,
    db_order_id,
    order_status,
    ns_custom_id,
    ns_price_usd,
    effective_funpay_lot_id,
):
    """NS create->pay->wait. Возвращает dict (терминал) либо
    (pins, ns_custom_id, ns_price_usd) на успехе."""
    # ─── 6. NS create_order (если ещё не создан) ───
    if order_status in ("received",) or ns_custom_id is None:
        try:
            ns_fields = _build_ns_fields(
                mapping.ns_fields_template, event.quantity
            )
        except ValueError as exc:
            error_text = f"Ошибка в шаблоне ns_fields_template: {exc}"
            log.error(error_text)
            await _mark_failed(
                db_order_id, error_text, telegram, event,
                funpay_client=funpay_client,
                funpay_lot_id=effective_funpay_lot_id,
                log=log,
            )
            return {"status": "failed", "reason": error_text}

        # Аудит #1: idempotency NS create_order.
        # 1) UUID4 custom_id, генерируется ОДИН раз и сразу пишется в БД
        #    — при retry мы возьмём этот же UUID и обратимся к ТОМУ ЖЕ
        #    NS-заказу, никакого UUID-дубля.
        #    NB: до 2026-05-25 здесь была детерминистическая схема
        #    `fp-{funpay_order_id}`, но NS обновил валидацию и теперь
        #    требует строго UUID4 ({400, custom_id must be a valid UUID4}).
        # 2) Intent marker: сохраняем UUID в БД ДО вызова NS, чтобы при
        #    crash до ответа retry уже знал, какой id проверять.
        # 3) Pre-check `order_info`: если предыдущий attempt успел дойти
        #    до NS — пропускаем create.
        if ns_custom_id is None or not _is_valid_uuid4(ns_custom_id):
            # Либо новый заказ, либо в БД остался legacy "fp-..." id
            # (с предыдущей версии кода). В обоих случаях генерим UUID4
            # и перезаписываем — старый "fp-..." не существует в NS,
            # обращение к нему по order_info даст 404 / валидационную
            # ошибку. Перегенерация безопасна, потому что create_order
            # для legacy-id всё равно бы провалился с 400.
            if ns_custom_id is not None:
                log.warning(
                    f"legacy ns_custom_id={ns_custom_id!r} в БД не UUID4 — "
                    "перегенерирую"
                )
            ns_custom_id = NSClient.new_custom_id()
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
            if db_order.ns_custom_id != ns_custom_id:
                await update_order(session, db_order, ns_custom_id=ns_custom_id)
                await session.commit()

        pre_info = await _ns_check_existing_order(ns_client, ns_custom_id, log)
        if pre_info is not None:
            log.info(
                f"NS idempotency: заказ {ns_custom_id} уже существует "
                f"(status={pre_info.status_enum}), create пропускаю"
            )
            if pre_info.total_price is not None:
                ns_price_usd = float(pre_info.total_price)
        else:
            try:
                created = await ns_client.create_order(
                    service_id=mapping.ns_service_id,
                    fields=ns_fields,
                    custom_id=ns_custom_id,
                )
            except NSError as exc:
                error_text = f"NS create_order упал: {exc}"
                log.error(error_text)
                await _mark_failed(
                    db_order_id, error_text, telegram, event,
                    funpay_client=funpay_client,
                    funpay_lot_id=effective_funpay_lot_id,
                    log=log,
                )
                return {"status": "failed", "reason": error_text}
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
        # Аудит #1: idempotency pay_order. Pre-check `order_info`:
        # если предыдущий pay уже дошёл до NS (status != CREATED),
        # повторный pay не нужен — переиспользуем существующий статус.
        # Это предотвращает потенциальное двойное списание.
        pre_info = await _ns_check_existing_order(ns_client, ns_custom_id, log)
        pre_status = pre_info.status_enum if pre_info is not None else None

        if pre_status in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
            msg = pre_info.status_message if pre_info else ""
            error_text = f"NS вернул возврат/отмену (idempotency check): {msg}"
            log.error(error_text)
            await _mark_failed(
                db_order_id, error_text, telegram, event,
                funpay_client=funpay_client,
                funpay_lot_id=effective_funpay_lot_id,
                log=log,
            )
            return {"status": "failed", "reason": error_text}

        if pre_status in (OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED):
            log.info(
                f"NS idempotency: pay_order пропускаю — заказ "
                f"{ns_custom_id} уже в статусе {pre_status.name}"
            )
            pins = list(pre_info.pins or []) if pre_info else []
        else:
            try:
                pay_resp = await ns_client.pay_order(ns_custom_id)
            except NSInsufficientFunds as exc:
                error_text = f"Недостаточно средств на NS: balance={exc.balance}"
                log.error(error_text)
                await _mark_failed(
                    db_order_id, error_text, telegram, event,
                    funpay_client=funpay_client,
                    funpay_lot_id=effective_funpay_lot_id,
                    log=log,
                )
                return {"status": "failed", "reason": error_text}
            except NSError as exc:
                error_text = f"NS pay_order упал: {exc}"
                log.error(error_text)
                await _mark_failed(
                    db_order_id, error_text, telegram, event,
                    funpay_client=funpay_client,
                    funpay_lot_id=effective_funpay_lot_id,
                    log=log,
                )
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
        # wait_order_completion внутри сам поллит NS до своего timeout'a
        # (NS_ORDER_TIMEOUT_SECONDS). Мы дополнительно усекаем его до
        # остатка до hard-timeout, чтобы не уходить за общий лимит
        # цикла received→delivered. min_wait = 10s, чтобы хотя бы один
        # poll-цикл успел отработать; иначе сразу manual_hold.
        wait_timeout: float | None = None
        if settings.order_delivery_hard_timeout_seconds > 0:
            async with session_factory()() as session:
                fresh = await find_order_by_funpay_id(
                    session, event.funpay_order_id
                )
            assert fresh is not None
            age = _order_age_seconds(fresh)
            remaining = (
                settings.order_delivery_hard_timeout_seconds - age
            )
            if remaining <= 10:
                return await _trigger_manual_hold(
                    funpay_order_id=event.funpay_order_id,
                    stage="ns_wait_completion",
                    reason=(
                        f"hard timeout до старта ожидания pins: "
                        f"age={int(age)}s, "
                        f"лимит={settings.order_delivery_hard_timeout_seconds}s"
                    ),
                    funpay_client=funpay_client,
                    telegram=telegram,
                    log=log,
                )
            wait_timeout = min(
                float(settings.ns_order_timeout_seconds), remaining
            )
        try:
            info = await ns_client.wait_order_completion(
                ns_custom_id, timeout_seconds=wait_timeout
            )
        except NSOrderTimeoutError as exc:
            # Деньги уже списаны в NS, но pins не пришли вовремя.
            # Это ровно тот сценарий, где нужен оператор: проверить
            # NS-кабинет/саппорт и решить, выдавать ли вручную.
            reason = f"NS не выдал коды за тайм-аут: {exc}"
            log.error(reason)
            return await _trigger_manual_hold(
                funpay_order_id=event.funpay_order_id,
                stage="ns_wait_completion",
                reason=reason,
                funpay_client=funpay_client,
                telegram=telegram,
                log=log,
            )
        except NSError as exc:
            # Аудит #6: NSAPIError (429 после retry-исчерпания, 5xx,
            # 4xx и т.п.) ПОСЛЕ pay_order. Деньги уже списаны, pins
            # не получены — оператор должен решить через Telegram.
            # До фикса: исключение вылетало наверх, статус оставался
            # ns_paid, никакого алерта.
            reason = f"NS вернул ошибку при ожидании pins: {exc}"
            log.error(reason)
            return await _trigger_manual_hold(
                funpay_order_id=event.funpay_order_id,
                stage="ns_wait_completion",
                reason=reason,
                funpay_client=funpay_client,
                telegram=telegram,
                log=log,
            )
        if info.status_enum == OrderStatus.COMPLETED and info.pins:
            pins = list(info.pins)
        elif info.status_enum in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
            error_text = f"NS вернул возврат/отмену: {info.status_message}"
            log.error(error_text)
            await _mark_failed(
                db_order_id, error_text, telegram, event,
                funpay_client=funpay_client,
                funpay_lot_id=effective_funpay_lot_id,
                log=log,
            )
            return {"status": "failed", "reason": error_text}

    if not pins:
        error_text = "NS заказ завершился, но pins пустой"
        log.error(error_text)
        await _mark_failed(
            db_order_id, error_text, telegram, event,
            funpay_client=funpay_client,
            funpay_lot_id=effective_funpay_lot_id,
            log=log,
        )
        return {"status": "failed", "reason": error_text}
    return pins, ns_custom_id, ns_price_usd
