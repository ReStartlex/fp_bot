"""
Общие данные FunPay-заказа для orders-пайплайна.

Вынесено из `processor.py` в отдельный leaf-модуль (P1-1), чтобы стадии
(`orders/stages/*`) и сам `processor` импортировали `FunPayOrderEvent`
без циклической зависимости. `processor` ре-экспортит его для обратной
совместимости (`from src.orders.processor import FunPayOrderEvent`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


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
    description: Optional[str] = None
