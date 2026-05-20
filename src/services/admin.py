"""Read-only admin сервисы для Telegram/Web UI."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select

from src.config import Settings, get_settings
from src.db.models import LotGroup, Mapping, Order, SyncRun
from src.db.session import session_factory
from src.mapping.rules import estimate_profit_rub
from src.sync.fx import get_rate_breakdown


ORDER_ACTIVE_STATUSES = ("received", "ns_created", "ns_paid", "pins_ready")
ORDER_PROBLEM_STATUSES = ("failed", "pins_ready")


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def serialize_order(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "funpay_order_id": order.funpay_order_id,
        "funpay_lot_id": order.funpay_lot_id,
        "ns_service_id": order.ns_service_id,
        "ns_custom_id": order.ns_custom_id,
        "buyer_username": order.buyer_username,
        "buyer_user_id": order.buyer_user_id,
        "chat_id": order.chat_id,
        "quantity": order.quantity,
        "funpay_price_rub": order.funpay_price_rub,
        "ns_price_usd": order.ns_price_usd,
        "fx_rate_at_sale": order.fx_rate_at_sale,
        "profit_rub": order.profit_rub,
        "profit_margin_percent": order.profit_margin_percent,
        "status": order.status,
        "error": order.error,
        "created_at": _dt(order.created_at),
        "updated_at": _dt(order.updated_at),
    }


def serialize_mapping(mapping: Mapping, group_name: str | None = None) -> dict[str, Any]:
    return {
        "id": mapping.id,
        "funpay_lot_id": mapping.funpay_lot_id,
        "ns_service_id": mapping.ns_service_id,
        "markup_percent": mapping.markup_percent,
        "stock_cap": mapping.stock_cap,
        "group_id": mapping.group_id,
        "group_name": group_name,
        "enabled": mapping.enabled,
        "label": mapping.label,
        "created_at": _dt(mapping.created_at),
        "updated_at": _dt(mapping.updated_at),
    }


async def get_dashboard_summary(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    async with session_factory()() as session:
        last_sync = (
            await session.execute(select(SyncRun).order_by(desc(SyncRun.started_at)).limit(1))
        ).scalar_one_or_none()
        order_counts = {
            status: int(count or 0)
            for status, count in (
                await session.execute(
                    select(Order.status, func.count(Order.id)).group_by(Order.status)
                )
            ).all()
        }
        mapping_counts = {
            bool(enabled): int(count or 0)
            for enabled, count in (
                await session.execute(
                    select(Mapping.enabled, func.count(Mapping.id)).group_by(Mapping.enabled)
                )
            ).all()
        }

    active_orders = sum(order_counts.get(status, 0) for status in ORDER_ACTIVE_STATUSES)
    problem_orders = sum(order_counts.get(status, 0) for status in ORDER_PROBLEM_STATUSES)
    return {
        "service": "funpay-ns-bot",
        "web_api_enabled": settings.web_api_enabled,
        "orders": {
            "total": sum(order_counts.values()),
            "active": active_orders,
            "problem": problem_orders,
            "by_status": order_counts,
        },
        "mappings": {
            "enabled": mapping_counts.get(True, 0),
            "disabled": mapping_counts.get(False, 0),
            "total": sum(mapping_counts.values()),
        },
        "sync": {
            "last_status": last_sync.status if last_sync is not None else None,
            "last_started_at": _dt(last_sync.started_at if last_sync is not None else None),
            "last_finished_at": _dt(last_sync.finished_at if last_sync is not None else None),
            "last_checked": last_sync.lots_checked if last_sync is not None else 0,
            "last_updated": last_sync.lots_updated if last_sync is not None else 0,
            "last_skipped": last_sync.lots_skipped if last_sync is not None else 0,
            "last_error": last_sync.error if last_sync is not None else None,
        },
        "guardrails": {
            "min_margin_percent": settings.sync_min_margin_percent,
            "max_price_change_percent": settings.sync_max_price_change_percent,
            "reserve_pending_orders": settings.sync_reserve_pending_orders,
        },
        "reconciler": {
            "enabled": settings.order_reconcile_enabled,
            "interval_seconds": settings.order_reconcile_interval_seconds,
            "stale_after_seconds": settings.order_reconcile_stale_after_seconds,
            "max_per_run": settings.order_reconcile_max_per_run,
        },
    }


async def list_orders(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    async with session_factory()() as session:
        stmt = select(Order).order_by(desc(Order.created_at)).limit(limit)
        if status:
            stmt = stmt.where(Order.status == status)
        rows = list((await session.execute(stmt)).scalars().all())
    return [serialize_order(row) for row in rows]


async def list_mappings(limit: int = 200, enabled: bool | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    async with session_factory()() as session:
        stmt = select(Mapping).order_by(Mapping.funpay_lot_id).limit(limit)
        if enabled is not None:
            stmt = stmt.where(Mapping.enabled.is_(enabled))
        rows = list((await session.execute(stmt)).scalars().all())
        group_ids = {row.group_id for row in rows if row.group_id is not None}
        groups: dict[int, str] = {}
        if group_ids:
            group_rows = (
                await session.execute(select(LotGroup).where(LotGroup.id.in_(group_ids)))
            ).scalars().all()
            groups = {row.id: row.name for row in group_rows}
    return [serialize_mapping(row, groups.get(row.group_id)) for row in rows]


async def list_problem_items(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    async with session_factory()() as session:
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.status.in_(ORDER_PROBLEM_STATUSES))
                    .order_by(desc(Order.updated_at))
                    .limit(limit)
                )
            ).scalars().all()
        )
        disabled_mappings = list(
            (
                await session.execute(
                    select(Mapping)
                    .where(Mapping.enabled.is_(False))
                    .order_by(Mapping.funpay_lot_id)
                    .limit(limit)
                )
            ).scalars().all()
        )
    return {
        "orders": [serialize_order(row) for row in orders],
        "disabled_mappings": [serialize_mapping(row) for row in disabled_mappings],
    }


async def get_profit_summary(days: int = 7, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    days = max(1, min(days, 90))
    rate = await get_rate_breakdown(settings)
    since = datetime.utcnow() - timedelta(days=days)
    async with session_factory()() as session:
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.status == "delivered")
                    .where(Order.created_at >= since)
                    .order_by(desc(Order.created_at))
                )
            ).scalars().all()
        )

    revenue = cost = profit = 0.0
    counted = exact_count = 0
    for order in orders:
        fx = order.fx_rate_at_sale or rate.effective
        estimated = estimate_profit_rub(order.funpay_price_rub, order.ns_price_usd, fx)
        if estimated is None:
            continue
        order_revenue, order_cost, order_profit, margin = estimated
        if order.profit_rub is not None:
            order_profit = order.profit_rub
            order_cost = order_revenue - order_profit
            exact_count += 1
        revenue += order_revenue
        cost += order_cost
        profit += order_profit
        counted += 1
    margin = profit / revenue * 100.0 if revenue > 0 else 0.0
    return {
        "days": days,
        "orders_counted": counted,
        "exact_orders": exact_count,
        "revenue_rub": revenue,
        "cost_rub": cost,
        "profit_rub": profit,
        "margin_percent": margin,
        "fallback_fx_rate": rate.effective,
    }
