"""
Синхронизация статуса «Оплачен» FunPay с нашей БД.

Когда саппорт FunPay подтверждает выданный нами заказ по нашему запросу
(после 24ч ожидания), он делает это **тихо**: статус заказа на FunPay
меняется на «Закрыт», но **системное сообщение** «Администратор X
подтвердил...» в чат покупателя НЕ отправляется (поведение FunPay
для batch-подтверждений). Поэтому `ChatHandler._handle_funpay_system_message`
ничего не ловит, и в БД заказ навсегда остаётся `delivered, confirmed_at=NULL`.

За пару месяцев работы накапливается сотня-другая таких «фантомных»
заказов, и команда `/pending_confirm` теряет ценность: показывает мусор
вместо реально ожидающих 20-30 свежих заказов.

Решение этой задачи — периодически (или по кнопке) скрапить страницу
funpay.com/orders/trade с фильтром state=paid и считать всё, чего там
нет, тихо подтверждённым саппортом.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger
from sqlalchemy import select

from src.db.models import Order
from src.db.repo import CONFIRMED_BY_ADMIN
from src.funpay.client import FunPayClient


SessionFactoryProvider = Callable[[], Any]


async def sync_pending_confirmation(
    *,
    funpay_client: FunPayClient,
    session_factory: SessionFactoryProvider,
) -> dict[str, int]:
    """
    Сверить delivered+NULL заказы в БД с актуальным «Оплачен» на FunPay.

    Алгоритм:
    1. Тянем с FunPay список всех заказов в статусе «Оплачен»
       (`funpay.com/orders/trade?state=paid`). Обычно это <50 шт.
    2. Берём из БД все Order со `status='delivered'` и `confirmed_at IS NULL`.
    3. Для каждого ордера из БД, чей funpay_order_id ОТСУТСТВУЕТ в
       списке paid → ставим `confirmed_at=NOW(), confirmed_by='admin'`.
       Это и есть тихое подтверждение саппортом.

    Уже подтверждённые заказы (`confirmed_at IS NOT NULL`) — НЕ трогаем,
    чтобы не перетереть, например, `confirmed_by='buyer'` (точное
    подтверждение через системку покупателя).

    Возвращает stats для отчёта в Telegram:
        {
            "paid_on_funpay": int,             # сколько сейчас в paid на FunPay
            "delivered_unconfirmed_in_db": int, # сколько было кандидатов в БД
            "marked_confirmed": int,           # сколько только что закрыли
        }
    """
    paid_ids = await funpay_client.get_paid_sales_snapshot()
    paid_set = set(paid_ids)

    factory = session_factory()
    async with factory() as session:
        stmt = (
            select(Order)
            .where(Order.status == "delivered")
            .where(Order.confirmed_at.is_(None))
        )
        result = await session.execute(stmt)
        candidates = list(result.scalars().all())

        now = datetime.utcnow()
        marked = 0
        for order in candidates:
            if order.funpay_order_id in paid_set:
                # Реально ещё ждёт подтверждения — оставляем.
                continue
            order.confirmed_at = now
            order.confirmed_by = CONFIRMED_BY_ADMIN
            marked += 1

        if marked:
            await session.commit()

    stats = {
        "paid_on_funpay": len(paid_set),
        "delivered_unconfirmed_in_db": len(candidates),
        "marked_confirmed": marked,
    }
    logger.info(
        f"sync_pending_confirmation: paid_on_funpay={stats['paid_on_funpay']}, "
        f"delivered_unconfirmed_in_db={stats['delivered_unconfirmed_in_db']}, "
        f"marked_confirmed={stats['marked_confirmed']}"
    )
    return stats
