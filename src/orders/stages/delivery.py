"""
Стадия delivery: двухфазная доставка кодов покупателю и гард ручной
интервенции. Вынесено из processor.py (P1-1) без изменения поведения.
"""
from __future__ import annotations

from datetime import timedelta

from src.alerts.telegram import TelegramNotifier
from src.chat import templates
from src.config import get_settings
from src.db.models import Order
from src.db.repo import (
    find_order_by_funpay_id,
    invalidate_mapping_cache_for_funpay_lot,
    update_order,
)
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.mapping.rules import estimate_profit_rub
from src.orders.events import FunPayOrderEvent
from src.orders.stages.holds import _emergency_disable_lot
from src.sync.fx import get_usd_rub_rate
from src.timeutil import utcnow


async def _should_hold_delivery(
    session,
    order: Order,
    *,
    grace_seconds: int | None = None,
    manual_guard_seconds: int | None = None,
) -> bool:
    """
    True если перед доставкой нужно остановиться и отдать заказ оператору.

    Защита от двойной выдачи с ночным grace-period: покупатель может написать
    !помощь сразу после оплаты, но бот всё ещё имеет окно на нормальную
    автовыдачу. После окна заказ уходит оператору, чтобы не догнать ручную
    выдачу дублем.
    """
    if order.status == "manual_hold":
        return True
    if order.chat_id is None:
        return False
    from src.db.models import ChatState

    state = await session.get(ChatState, order.chat_id)
    if state is None:
        return False
    created_at = order.created_at
    # SQLite server_default в тестах иногда отдаёт naive datetime — сравниваем
    # naive UTC, как и остальной код в проекте.
    manual_at = getattr(state, "last_manual_message_at", None)
    if manual_at is not None:
        if manual_at >= created_at:
            return True
        if manual_guard_seconds is None:
            manual_guard_seconds = int(
                get_settings().order_manual_intervention_guard_seconds
            )
        if manual_guard_seconds <= 0:
            return True
        if created_at - manual_at <= timedelta(seconds=manual_guard_seconds):
            return True

    if state.last_help_request_at is None:
        return False
    if state.last_help_request_at < created_at:
        return False
    if grace_seconds is None:
        grace_seconds = int(get_settings().chat_help_auto_delivery_grace_seconds)
    if grace_seconds <= 0:
        return True
    return utcnow() >= created_at + timedelta(seconds=grace_seconds)


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
    force_delivery: bool = False,
    help_grace_seconds: int | None = None,
) -> dict:
    """
    Шаг доставки: отправляет коды в чат FunPay, обновляет статус.
    На вход подаются уже сохранённые pins. Если FunPay упал — статус
    останется pins_ready, повторим в следующий вход.
    """
    ns_custom_id = ns_custom_id or db_order.ns_custom_id
    ns_price_usd = ns_price_usd if ns_price_usd is not None else db_order.ns_price_usd

    async with session_factory()() as session:
        latest = await find_order_by_funpay_id(session, event.funpay_order_id)
        # ── Защита от гонки с оператором ──
        # Между моментом, когда мы вошли в _deliver_pins, и моментом
        # send_message, оператор мог в Telegram нажать «✅ Выдано вручную»
        # (или «Retry» с уже delivered'нутыми pins). В этом случае
        # автоматическая повторная отправка = дубль = потеря денег.
        # Гард срабатывает ДАЖЕ при force_delivery: если оператор уже
        # пометил выдано вручную, никакой Retry не должен переотправить.
        if latest is not None and latest.status == "delivered":
            log.warning(
                "Заказ уже delivered (вероятно оператор подтвердил вручную) — "
                "не отправляю pins повторно (force_delivery="
                f"{force_delivery})"
            )
            return {
                "status": "delivered",
                "ns_custom_id": ns_custom_id,
                "pins_count": len(pins),
                "skipped": True,
                "reason": "already delivered by operator",
            }
        if (
            latest is not None
            and not force_delivery
            and await _should_hold_delivery(
                session,
                latest,
                grace_seconds=help_grace_seconds,
                manual_guard_seconds=int(
                    get_settings().order_manual_intervention_guard_seconds
                ),
            )
        ):
            await update_order(
                session,
                latest,
                status="manual_hold",
                error=(
                    latest.error
                    or "manual_hold: help/manual intervention; автодоставка остановлена"
                ),
            )
            await session.commit()
            if telegram is not None:
                await telegram.warning(
                    f"🛑 Не отправляю pins по заказу "
                    f"<code>{event.funpay_order_id}</code>: активна ручная проверка."
                )
            return {
                "status": "manual_hold",
                "ns_custom_id": ns_custom_id,
                "pins_count": len(pins),
                "reason": "manual hold",
            }

    delivery_text = templates.delivery(event.buyer_username or "друг", pins)

    if funpay_client is None or event.chat_id is None:
        log.warning(
            "FunPay-клиент или chat_id отсутствуют — доставка отложена; "
            "статус остаётся pins_ready"
        )
        await _emergency_disable_lot(
            db_order.funpay_lot_id,
            funpay_client,
            telegram,
            reason="pins_ready: нет FunPay-клиента или chat_id для доставки",
            log=log,
        )
        return {
            "status": "pins_ready",
            "ns_custom_id": ns_custom_id,
            "pins_count": len(pins),
            "reason": "no funpay client or chat_id",
        }

    # Аудит #3: двухфазная доставка. ДО send_message — статус `delivering`.
    # Это intent-marker «попытка отправки в процессе». Если процесс упадёт
    # между success send_message и commit'ом delivered, статус останется
    # delivering, и reconciler НЕ повторит отправку автоматически
    # (риск двойной выдачи) — переведёт в manual_hold для оператора.
    async with session_factory()() as session:
        order = await find_order_by_funpay_id(session, event.funpay_order_id)
        if order is not None:
            await update_order(session, order, status="delivering")
            await session.commit()

    try:
        await funpay_client.send_message(event.chat_id, delivery_text)
    except Exception as exc:
        # send_message бросил — сообщение НЕ ушло. Безопасно откатить
        # на pins_ready, чтобы reconciler/Retry могли попробовать ещё раз.
        log.error(f"Доставка в чат FunPay упала: {exc}; откат на pins_ready")
        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, event.funpay_order_id)
            if order is not None and order.status == "delivering":
                await update_order(session, order, status="pins_ready")
                await session.commit()
        await _emergency_disable_lot(
            db_order.funpay_lot_id,
            funpay_client,
            telegram,
            reason=f"pins_ready: доставка в чат FunPay упала: {exc}",
            log=log,
        )
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
    fx_rate_at_sale: float | None = None
    profit_rub: float | None = None
    profit_margin_percent: float | None = None
    try:
        settings = get_settings()
        fx_rate_at_sale = await get_usd_rub_rate(settings)
        estimated = estimate_profit_rub(
            event.funpay_price_rub,
            ns_price_usd,
            fx_rate_at_sale,
            withdrawal_fee_percent=settings.funpay_withdrawal_fee_percent,
        )
        if estimated is not None:
            _, _, profit_rub, profit_margin_percent = estimated
    except Exception as exc:
        log.warning(f"Не смог посчитать точную прибыль заказа: {exc}")
    async with session_factory()() as session:
        order = await find_order_by_funpay_id(session, event.funpay_order_id)
        assert order is not None
        await update_order(
            session,
            order,
            status="delivered",
            fx_rate_at_sale=fx_rate_at_sale,
            profit_rub=profit_rub,
            profit_margin_percent=profit_margin_percent,
        )
        # Инвалидация diff-cache. FunPay при продаже САМ списывает сток
        # (100→97), наш target = min(NS, cap) = 100 не меняется, поэтому
        # без инвалидации diff-cache видит совпадение и пропускает sync
        # — FunPay-сток так и торчит на 97 до истечения TTL. Сбрасываем
        # last_synced_at, чтобы следующий sync-цикл (≤30с) пошёл через
        # реальный FunPay GET и поднял сток обратно к target.
        #
        # ВАЖНО: используем `order.funpay_lot_id`, а НЕ `event.funpay_lot_id`.
        # Для заказов, пришедших через chat handler / order discovery,
        # event.funpay_lot_id может быть 0 (FunPayAPI часто не отдаёт
        # lot_id в OrderShortcut). В таком случае мы матчили лот по
        # описанию в _resolve_mapping, и сохранили в БД именно
        # эффективный lot_id из mapping'а — его и используем.
        effective_lot_id = order.funpay_lot_id or event.funpay_lot_id
        if effective_lot_id and effective_lot_id > 0:
            try:
                await invalidate_mapping_cache_for_funpay_lot(
                    session, funpay_lot_id=effective_lot_id
                )
            except Exception as exc:
                log.warning(
                    f"invalidate_mapping_cache_for_funpay_lot упал "
                    f"(lot={effective_lot_id}): {exc}"
                )
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
