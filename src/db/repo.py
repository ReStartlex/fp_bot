"""Высокоуровневые операции с БД."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    ChatState,
    FunpayChatCursor,
    FxRate,
    LotGroup,
    Mapping,
    Order,
    SyncRun,
)
from src.mapping.groups import (
    DEFAULT_LOT_GROUPS,
    group_keywords_to_text,
    group_match_score,
)


# ---------- Mappings ----------

async def list_mappings(
    session: AsyncSession,
    *,
    only_enabled: bool = True,
    group_id: int | None = None,
) -> list[Mapping]:
    stmt = select(Mapping)
    if only_enabled:
        stmt = stmt.where(Mapping.enabled.is_(True))
    if group_id is not None:
        stmt = stmt.where(Mapping.group_id == group_id)
    stmt = stmt.order_by(Mapping.funpay_lot_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def upsert_mapping(
    session: AsyncSession,
    *,
    funpay_lot_id: int,
    ns_service_id: int,
    markup_percent: float | None = None,
    stock_cap: int | None = None,
    ns_fields_template: str | None = None,
    enabled: bool = True,
    label: str | None = None,
    group_id: int | None = None,
) -> Mapping:
    existing = await session.execute(
        select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
    )
    obj = existing.scalar_one_or_none()
    if obj is None:
        obj = Mapping(funpay_lot_id=funpay_lot_id, ns_service_id=ns_service_id)
        session.add(obj)
    obj.ns_service_id = ns_service_id
    obj.markup_percent = markup_percent
    obj.stock_cap = stock_cap
    obj.ns_fields_template = ns_fields_template
    obj.enabled = enabled
    obj.label = label
    if group_id is not None:
        obj.group_id = group_id
    await session.flush()
    return obj


# ---------- Lot groups ----------

async def ensure_default_lot_groups(session: AsyncSession) -> list[LotGroup]:
    result = await session.execute(select(LotGroup))
    existing = {row.slug: row for row in result.scalars().all()}
    changed: list[LotGroup] = []
    for item in DEFAULT_LOT_GROUPS:
        group = existing.get(item.slug)
        if group is None:
            group = LotGroup(
                slug=item.slug,
                name=item.name,
                match_keywords=group_keywords_to_text(item.keywords),
                markup_percent=item.markup_percent,
                stock_cap=item.stock_cap,
                sort_order=item.sort_order,
                enabled=True,
            )
            session.add(group)
            changed.append(group)
            continue
        group.name = group.name or item.name
        if not group.match_keywords:
            group.match_keywords = group_keywords_to_text(item.keywords)
        group.sort_order = group.sort_order or item.sort_order
    await session.flush()
    return changed


async def list_lot_groups(
    session: AsyncSession, *, only_enabled: bool = False
) -> list[LotGroup]:
    await ensure_default_lot_groups(session)
    stmt = select(LotGroup)
    if only_enabled:
        stmt = stmt.where(LotGroup.enabled.is_(True))
    stmt = stmt.order_by(LotGroup.sort_order, LotGroup.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_lot_group_by_id(session: AsyncSession, group_id: int) -> LotGroup | None:
    return await session.get(LotGroup, group_id)


async def classify_lot_group(
    session: AsyncSession, text: str | None
) -> LotGroup | None:
    groups = await list_lot_groups(session, only_enabled=True)
    scored = [
        (group_match_score(text, group.match_keywords), group)
        for group in groups
    ]
    scored = [(score, group) for score, group in scored if score > 0]
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], -item[1].sort_order), reverse=True)
    return scored[0][1]


async def assign_mapping_group(
    session: AsyncSession, mapping: Mapping, group_id: int | None
) -> Mapping:
    mapping.group_id = group_id
    await session.flush()
    return mapping


async def set_lot_group_markup(
    session: AsyncSession, group_id: int, markup_percent: float | None
) -> LotGroup | None:
    group = await find_lot_group_by_id(session, group_id)
    if group is None:
        return None
    group.markup_percent = markup_percent
    await session.flush()
    return group


# ---------- FX rates ----------

async def save_fx_rate(
    session: AsyncSession, *, pair: str, rate: float, source: str | None = None
) -> FxRate:
    obj = FxRate(pair=pair, rate=rate, source=source)
    session.add(obj)
    await session.flush()
    return obj


async def latest_fx_rate(session: AsyncSession, pair: str) -> FxRate | None:
    stmt = select(FxRate).where(FxRate.pair == pair).order_by(FxRate.fetched_at.desc()).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ---------- Sync runs ----------

async def start_sync_run(session: AsyncSession) -> SyncRun:
    obj = SyncRun()
    session.add(obj)
    await session.flush()
    return obj


async def finish_sync_run(
    session: AsyncSession,
    run: SyncRun,
    *,
    status: str,
    lots_checked: int = 0,
    lots_updated: int = 0,
    lots_skipped: int = 0,
    error: str | None = None,
) -> None:
    """
    Завершить SyncRun. Корректно работает, даже если `run` создан в
    другой сессии: подтягиваем актуальный экземпляр через session.get
    по PK, иначе SQLAlchemy будет ругаться на detached object.
    """
    target = run
    if run.id is not None:
        loaded = await session.get(SyncRun, run.id)
        if loaded is not None:
            target = loaded
    target.finished_at = datetime.utcnow()
    target.status = status
    target.lots_checked = lots_checked
    target.lots_updated = lots_updated
    target.lots_skipped = lots_skipped
    target.error = error
    await session.flush()


# ---------- Orders ----------

async def find_order_by_funpay_id(session: AsyncSession, funpay_order_id: str) -> Order | None:
    stmt = select(Order).where(Order.funpay_order_id == funpay_order_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_order(
    session: AsyncSession,
    *,
    funpay_order_id: str,
    funpay_lot_id: int,
    ns_service_id: int,
    buyer_username: str | None,
    buyer_user_id: int | None,
    chat_id: int | None,
    quantity: int,
    funpay_price_rub: float | None,
    description: str | None = None,
) -> Order:
    obj = Order(
        funpay_order_id=funpay_order_id,
        funpay_lot_id=funpay_lot_id,
        ns_service_id=ns_service_id,
        buyer_username=buyer_username,
        buyer_user_id=buyer_user_id,
        chat_id=chat_id,
        quantity=quantity,
        funpay_price_rub=funpay_price_rub,
        description=description,
        status="received",
    )
    session.add(obj)
    await session.flush()
    return obj


async def update_order(session: AsyncSession, order: Order, **fields: Any) -> Order:
    for key, value in fields.items():
        if key == "pins":
            order.pins_json = json.dumps(value, ensure_ascii=False) if value is not None else None
        else:
            setattr(order, key, value)
    await session.flush()
    return order


ACTIVE_ORDER_STATUSES = ("received", "ns_created", "ns_paid", "pins_ready", "manual_hold")


async def reserved_quantities_by_service(
    session: AsyncSession,
    *,
    statuses: tuple[str, ...] = ACTIVE_ORDER_STATUSES,
) -> dict[int, int]:
    """Сколько единиц уже занято активными заказами по NS service_id."""
    stmt = (
        select(Order.ns_service_id, func.coalesce(func.sum(Order.quantity), 0))
        .where(Order.status.in_(statuses))
        .where(Order.ns_service_id > 0)
        .group_by(Order.ns_service_id)
    )
    result = await session.execute(stmt)
    return {int(service_id): int(quantity or 0) for service_id, quantity in result.all()}


async def list_reconcilable_orders(
    session: AsyncSession,
    *,
    stale_after_seconds: int,
    limit: int,
) -> list[Order]:
    """Заказы, которые можно безопасно повторно прогнать через processor."""
    cutoff = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
    stmt = (
        select(Order)
        .where(Order.status.in_(("ns_created", "ns_paid", "pins_ready")))
        .where(Order.updated_at <= cutoff)
        .order_by(Order.updated_at, Order.id)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_active_orders_for_chat(
    session: AsyncSession,
    *,
    chat_id: int,
) -> list[Order]:
    """Активные заказы покупателя в чате, включая ручной hold."""
    stmt = (
        select(Order)
        .where(Order.chat_id == chat_id)
        .where(Order.status.in_(ACTIVE_ORDER_STATUSES))
        .order_by(Order.updated_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def hold_active_orders_for_chat(
    session: AsyncSession,
    *,
    chat_id: int,
    reason: str,
    grace_seconds: int = 0,
) -> list[Order]:
    """
    Перевести активные заказы чата в ручной hold.

    Это защита от двойной выдачи: если покупатель написал !помощь и оператор
    подключился вручную, автоматическая доставка больше не должна "догонять"
    этот чат без явного решения оператора.
    """
    orders = await list_active_orders_for_chat(session, chat_id=chat_id)
    now = datetime.utcnow()
    held: list[Order] = []
    for order in orders:
        if grace_seconds > 0:
            age_seconds = (now - order.created_at).total_seconds()
            if age_seconds < grace_seconds and order.status != "manual_hold":
                continue
        if order.status != "manual_hold":
            order.status = "manual_hold"
        order.error = reason
        held.append(order)
    await session.flush()
    return held


# ---------- Chat state ----------

async def get_or_create_chat_state(
    session: AsyncSession,
    *,
    chat_id: int,
    buyer_username: str | None,
) -> ChatState:
    stmt = select(ChatState).where(ChatState.chat_id == chat_id)
    state = (await session.execute(stmt)).scalar_one_or_none()
    if state is None:
        state = ChatState(chat_id=chat_id, buyer_username=buyer_username)
        session.add(state)
        await session.flush()
    elif buyer_username and state.buyer_username != buyer_username:
        state.buyer_username = buyer_username
        await session.flush()
    return state


async def mark_greeted(session: AsyncSession, state: ChatState) -> None:
    from datetime import datetime

    state.greeted_at = datetime.utcnow()
    await session.flush()


async def mark_help_requested(session: AsyncSession, state: ChatState) -> None:
    from datetime import datetime

    state.last_help_request_at = datetime.utcnow()
    state.help_requests_count = (state.help_requests_count or 0) + 1
    await session.flush()


async def mark_paid_order_seen(session: AsyncSession, state: ChatState) -> None:
    """Запомнить, что в чате было системное сообщение об оплате заказа."""
    state.last_paid_order_at = datetime.utcnow()
    await session.flush()


async def mark_manual_intervention(session: AsyncSession, state: ChatState) -> None:
    """Запомнить ручное исходящее сообщение продавца в чат."""
    state.last_manual_message_at = datetime.utcnow()
    state.manual_messages_count = (state.manual_messages_count or 0) + 1
    await session.flush()


# ---------- FunPay chat cursors ----------

async def get_chat_cursor(
    session: AsyncSession, chat_id: int
) -> FunpayChatCursor | None:
    """Курсор последнего обработанного сообщения для чата (или None)."""
    stmt = select(FunpayChatCursor).where(FunpayChatCursor.chat_id == chat_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_chat_cursor(
    session: AsyncSession,
    *,
    chat_id: int,
    last_message_id: int | None,
    last_message_text_hash: int | None = None,
) -> FunpayChatCursor:
    """
    Записать/обновить курсор. Если запись существует — обновляем,
    но НЕ откатываем last_message_id назад (это защита от случайного
    «забывания» — мы должны двигаться только вперёд).
    """
    cursor = await get_chat_cursor(session, chat_id)
    if cursor is None:
        cursor = FunpayChatCursor(
            chat_id=chat_id,
            last_message_id=last_message_id,
            last_message_text_hash=last_message_text_hash,
        )
        session.add(cursor)
        await session.flush()
        return cursor

    if last_message_id is not None and (
        cursor.last_message_id is None
        or last_message_id > cursor.last_message_id
    ):
        cursor.last_message_id = last_message_id
    if last_message_text_hash is not None:
        cursor.last_message_text_hash = last_message_text_hash
    await session.flush()
    return cursor


async def list_chat_cursors(
    session: AsyncSession,
) -> list[FunpayChatCursor]:
    stmt = select(FunpayChatCursor)
    return list((await session.execute(stmt)).scalars().all())
