"""Клиент ns.gifts (HMAC v2)."""
from src.ns.client import NSClient
from src.ns.exceptions import NSAPIError, NSAuthError, NSConflictError, NSInsufficientFunds

__all__ = [
    "NSClient",
    "NSAPIError",
    "NSAuthError",
    "NSConflictError",
    "NSInsufficientFunds",
]
