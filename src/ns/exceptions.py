"""Иерархия исключений NS-клиента."""
from __future__ import annotations


class NSError(Exception):
    """Базовая ошибка NS API."""


class NSAPIError(NSError):
    """HTTP-ошибка от ns.gifts."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        response_body: str | None = None,
        path: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.response_body = response_body
        self.path = path
        super().__init__(f"[{status_code}] {path or ''} {message}")


class NSAuthError(NSAPIError):
    """401: подпись/таймстамп/токен невалидны."""


class NSForbiddenError(NSAPIError):
    """403: IP не в whitelist или нет прав."""


class NSNotFoundError(NSAPIError):
    """404: заказ или сервис не найден."""


class NSConflictError(NSAPIError):
    """409: дубликат заказа или повторная оплата."""


class NSTotpRequiredError(NSAPIError):
    """428: нужен TOTP-код для покупки."""


class NSInsufficientFunds(NSError):
    """Недостаточно средств для оплаты заказа (status=insufficient)."""

    def __init__(self, custom_id: str, balance: str | None = None) -> None:
        self.custom_id = custom_id
        self.balance = balance
        super().__init__(
            f"Недостаточно средств для заказа {custom_id} (balance={balance})"
        )


class NSOrderTimeoutError(NSError):
    """Заказ не перешёл в финальный статус за NS_ORDER_TIMEOUT_SECONDS."""


def from_status_code(
    status_code: int, message: str, *, response_body: str | None = None, path: str | None = None
) -> NSAPIError:
    cls_map: dict[int, type[NSAPIError]] = {
        401: NSAuthError,
        403: NSForbiddenError,
        404: NSNotFoundError,
        409: NSConflictError,
        428: NSTotpRequiredError,
    }
    cls = cls_map.get(status_code, NSAPIError)
    return cls(status_code, message, response_body=response_body, path=path)
