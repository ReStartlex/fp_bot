"""Клиент FunPay (неофициальный, через golden_key) + watcher."""
from src.funpay.client import FunPayClient

# FunPayWatcher импортируется напрямую (from src.funpay.watcher import ...),
# чтобы избежать циклического импорта с src.orders.processor.

__all__ = ["FunPayClient"]
