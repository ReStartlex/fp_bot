"""Типы данных ns.gifts API v2."""
from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FieldType(BaseModel):
    """Схема одного поля заказа для категории."""
    key: str
    type: str
    name: str
    required: bool = True
    min: float | int | None = None
    max: float | int | None = None
    step: float | int | None = None
    regex: str | None = None
    enum: list[str] | None = None


class Service(BaseModel):
    """Конкретный товар/услуга в каталоге."""
    service_id: int
    service_name: str
    price: float
    currency: str
    in_stock: int


class Category(BaseModel):
    """Категория с массивом services и схемой fields."""
    category_id: int
    category_name: str
    services: list[Service] = Field(default_factory=list)
    fields: list[FieldType] = Field(default_factory=list)


class StockResponse(BaseModel):
    categories: list[Category] = Field(default_factory=list)


class OrderField(BaseModel):
    key: str
    value: Any


class CreateOrderRequest(BaseModel):
    service_id: int
    custom_id: str
    fields: list[OrderField]


class CreateOrderResponse(BaseModel):
    custom_id: str
    total_to_pay: str
    status: Literal["created"] = "created"


class PayOrderResponse(BaseModel):
    custom_id: str
    status: Literal["completed", "refunded", "in_progress", "insufficient"]
    balance: str | None = None
    pins: list[str] | None = None
    note: str | None = None
    data: Any | None = None


class OrderStatus(IntEnum):
    CREATED = 0
    IN_PROGRESS = 10
    COMPLETED = 2
    REFUNDED = 7
    CANCELLED = 5


class OrderInfo(BaseModel):
    custom_id: str
    status: int
    status_message: str
    product: str | None = None
    quantity: float | None = None
    total_price: float | None = None
    date: datetime | None = None
    pins: list[str] | None = None
    data: Any | None = None

    @property
    def status_enum(self) -> OrderStatus | None:
        try:
            return OrderStatus(self.status)
        except ValueError:
            return None


class BalanceResponse(BaseModel):
    balance: str


class TokenResponse(BaseModel):
    user_id: int
    token: str
    expires_in: int


class ExchangeRates(BaseModel):
    rub: float
    kzt: float | None = None
    uah: float | None = None


class ExchangeRateResponse(BaseModel):
    service_id: int
    date: datetime
    rates: ExchangeRates
