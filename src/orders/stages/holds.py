"""
Стадия holds: перевод заказа в manual_hold / failed и аварийная
деактивация лота. Вынесено из processor.py (P1-1) без изменения поведения.
"""
from __future__ import annotations

from sqlalchemy import select

from src.alerts.telegram import TelegramNotifier
from src.db.models import Mapping
from src.db.repo import find_order_by_funpay_id, update_order
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.orders.events import FunPayOrderEvent
from src.orders.stages.common import _order_age_seconds, _pins_from_order


async def _trigger_manual_hold(
    *,
    funpay_order_id: str,
    stage: str,
    reason: str,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    log,
) -> dict:
    """
    Перевести заказ в manual_hold по hard-timeout / NS-timeout.

    Что делает (в этом порядке):
      1) update_order(status='manual_hold', error=reason) — атомарно;
      2) пихает алерт в Telegram с кнопками retry/done/details;
      3) аварийно выключает FunPay-лот, чтобы новые покупатели не
         попадали на тот же узкий участок;
      4) возвращает stable-словарь {status: manual_hold, reason, ...}
         для возврата из process_funpay_order.

    Безопасно вызывать многократно: повторный manual_hold для уже
    held заказа просто перепишет error/timestamp, дублирующий
    Telegram-алерт оператор просто проигнорирует.
    """
    has_pins = False
    ns_custom_id: str | None = None
    buyer_username: str | None = None
    funpay_lot_id: int | None = None
    age_seconds = 0
    async with session_factory()() as session:
        order = await find_order_by_funpay_id(session, funpay_order_id)
        if order is not None:
            await update_order(
                session,
                order,
                status="manual_hold",
                error=f"{stage}: {reason}",
            )
            await session.commit()
            has_pins = bool(_pins_from_order(order))
            ns_custom_id = order.ns_custom_id
            buyer_username = order.buyer_username
            funpay_lot_id = order.funpay_lot_id
            age_seconds = int(_order_age_seconds(order))

    if telegram is not None:
        try:
            await telegram.manual_hold_required(
                funpay_order_id=funpay_order_id,
                stage=stage,
                age_seconds=age_seconds,
                buyer_username=buyer_username,
                ns_custom_id=ns_custom_id,
                has_pins=has_pins,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — diagnostics, alert не критичен
            log.warning(f"Не смог отправить manual_hold alert в Telegram: {exc}")

    await _emergency_disable_lot(
        funpay_lot_id,
        funpay_client,
        telegram,
        reason=f"manual_hold ({stage}): {reason}",
        log=log,
    )

    log.warning(
        f"manual_hold выставлен: stage={stage}, age={age_seconds}s, "
        f"ns_custom_id={ns_custom_id}, has_pins={has_pins}, reason={reason}"
    )
    return {
        "status": "manual_hold",
        "reason": reason,
        "stage": stage,
        "ns_custom_id": ns_custom_id,
        "has_pins": has_pins,
    }


async def _mark_failed(
    db_order_id: int | None,
    reason: str,
    telegram: TelegramNotifier | None,
    event: FunPayOrderEvent,
    *,
    funpay_client: FunPayClient | None = None,
    funpay_lot_id: int | None = None,
    log=None,
) -> None:
    if db_order_id is not None:
        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, event.funpay_order_id)
            if order is not None:
                await update_order(session, order, status="failed", error=reason)
                await session.commit()
                if funpay_lot_id is None:
                    funpay_lot_id = order.funpay_lot_id
    if telegram is not None:
        await telegram.order_failure(
            funpay_order_id=event.funpay_order_id, reason=reason
        )
    await _emergency_disable_lot(
        funpay_lot_id if funpay_lot_id is not None else event.funpay_lot_id,
        funpay_client,
        telegram,
        reason=reason,
        log=log,
    )


async def _emergency_disable_lot(
    funpay_lot_id: int | None,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    *,
    reason: str,
    log=None,
) -> bool:
    """
    Fail-safe: если автоматическая выдача по лоту сломалась, лот надо
    немедленно убрать из продажи, чтобы следующие покупатели не продолжили
    покупать проблемный товар.
    """
    if funpay_lot_id is None or funpay_lot_id <= 0:
        if log is not None:
            log.warning(
                f"Не могу аварийно выключить лот: неизвестный lot_id "
                f"(reason={reason})"
            )
        return False
    if funpay_client is None:
        if log is not None:
            log.warning(
                f"Не могу аварийно выключить лот {funpay_lot_id}: "
                "FunPay-клиент отсутствует"
            )
        return False

    funpay_disabled = False
    funpay_error: str | None = None

    try:
        lot_fields = await funpay_client.get_lot_fields(funpay_lot_id)
        # ВАЖНО: количество (amount) НЕ трогаем. FunPay не принимает
        # amount=0 — раньше форма с нулём молча отбраковывалась сервером,
        # save отвечал «успехом», и лот оставался активным (инцидент
        # 2026-06-07, заказ V38FGNF1). Деактивация = только снять
        # галочку «Активное», ровно как в UI.
        if hasattr(lot_fields, "active"):
            lot_fields.active = False
        result = await funpay_client.save_lot(lot_fields)
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(result)
        # Verify-after-save: FunPay умеет ответить «успехом», не применив
        # форму. Перечитываем лот и убеждаемся, что он реально выключен.
        verify = await funpay_client.get_lot_fields(funpay_lot_id)
        if bool(getattr(verify, "active", False)):
            raise RuntimeError(
                "save_lot отчитался успехом, но лот на FunPay всё ещё "
                "активен (FunPay не применил деактивацию)"
            )
        funpay_disabled = True
    except Exception as exc:
        funpay_error = str(exc)
        if log is not None:
            log.opt(exception=exc).error(
                f"Не смог аварийно выключить FunPay-лот {funpay_lot_id}: {exc}"
            )

    # Аудит #8: даже если save_lot на FunPay упал, отключаем mapping в БД.
    # Иначе sync_stock на следующем цикле увидит mapping.enabled=True и
    # «починит» лот обратно — проблемный лот продолжит продаваться,
    # копируя новые failed-заказы.
    mapping_disabled_in_db = False
    try:
        async with session_factory()() as session:
            mapping = (
                await session.execute(
                    select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
                )
            ).scalar_one_or_none()
            if mapping is not None and mapping.enabled:
                mapping.enabled = False
                await session.commit()
                mapping_disabled_in_db = True
    except Exception as exc:
        if log is not None:
            log.opt(exception=exc).error(
                f"Не смог отключить mapping в БД для FunPay-лота "
                f"{funpay_lot_id}: {exc}"
            )

    if not funpay_disabled:
        if telegram is not None:
            extra = (
                "Локальный mapping отключён — sync лот обратно не включит, "
                "но вручную через FunPay UI лот всё ещё активен."
                if mapping_disabled_in_db
                else "ВНИМАНИЕ: и mapping в БД отключить не удалось."
            )
            await telegram.error(
                f"🚨 Не смог аварийно выключить FunPay-лот "
                f"<code>{funpay_lot_id}</code>: "
                f"<code>{(funpay_error or '?')[:300]}</code>. {extra}"
            )
        return False

    if log is not None:
        log.warning(
            f"FunPay-лот {funpay_lot_id} аварийно выключен после ошибки: {reason}"
        )
    if telegram is not None:
        await telegram.warning(
            f"FunPay-лот <code>{funpay_lot_id}</code> аварийно выключен. "
            f"Локальный маппинг тоже отключён, чтобы sync не включил лот "
            f"обратно до ручной проверки. Причина: <code>{reason[:300]}</code>"
        )
    return True
