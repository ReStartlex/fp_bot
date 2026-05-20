"""Фоновое восстановление заказов, которые застряли между шагами pipeline."""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from src.alerts.telegram import TelegramNotifier
from src.config import Settings, get_settings
from src.db.repo import list_reconcilable_orders
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.ns import NSClient
from src.orders.processor import FunPayOrderEvent, process_funpay_order


@dataclass(frozen=True)
class ReconcileResult:
    checked: int = 0
    recovered: int = 0
    skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "recovered": self.recovered,
            "skipped": self.skipped,
            "failed": self.failed,
        }


async def reconcile_orders_once(
    *,
    settings: Settings | None = None,
    ns_client: NSClient | None = None,
    funpay_client: FunPayClient | None = None,
    telegram: TelegramNotifier | None = None,
) -> dict[str, int]:
    """Повторно прогнать stale-заказы в безопасных промежуточных статусах."""
    settings = settings or get_settings()
    if not settings.order_reconcile_enabled:
        return ReconcileResult().as_dict()

    async with session_factory()() as session:
        orders = await list_reconcilable_orders(
            session,
            stale_after_seconds=settings.order_reconcile_stale_after_seconds,
            limit=settings.order_reconcile_max_per_run,
        )

    checked = recovered = skipped = failed = 0
    for order in orders:
        checked += 1
        event = FunPayOrderEvent(
            funpay_order_id=order.funpay_order_id,
            funpay_lot_id=order.funpay_lot_id,
            buyer_username=order.buyer_username,
            buyer_user_id=order.buyer_user_id,
            chat_id=order.chat_id,
            quantity=order.quantity,
            funpay_price_rub=order.funpay_price_rub,
            description=None,
        )
        try:
            result = await process_funpay_order(
                event,
                settings=settings,
                ns_client=ns_client,
                funpay_client=funpay_client,
                telegram=telegram,
            )
        except Exception as exc:
            logger.exception(
                f"Reconciler упал на заказе {order.funpay_order_id}: {exc}"
            )
            failed += 1
            continue

        status = result.get("status")
        if status == "delivered":
            recovered += 1
        elif status in {"pins_ready", "ns_created", "ns_paid"}:
            skipped += 1
        elif status == "failed":
            failed += 1
        else:
            skipped += 1

    if checked:
        logger.info(
            f"Reconciler done: checked={checked}, recovered={recovered}, "
            f"skipped={skipped}, failed={failed}"
        )
    return ReconcileResult(
        checked=checked, recovered=recovered, skipped=skipped, failed=failed
    ).as_dict()
