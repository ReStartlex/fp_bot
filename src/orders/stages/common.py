"""
Общие мелкие хелперы стадий orders-пайплайна (P1-1 leaf-модуль, без
зависимостей от processor — чтобы стадии и сам processor импортировали
их без циклов).
"""
from __future__ import annotations

import json
from datetime import datetime

from src.db.models import Order
from src.timeutil import utcnow


def _pins_from_order(order: Order) -> list:
    """Прочитать список пинов из Order.pins_json."""
    if not order.pins_json:
        return []
    try:
        data = json.loads(order.pins_json)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _order_age_seconds(order: Order, *, now: datetime | None = None) -> float:
    """Сколько секунд прошло от Order.created_at. naive UTC, как и весь проект."""
    current = now or utcnow()
    return max(0.0, (current - order.created_at).total_seconds())
