"""
Тест парсинга баланса FunPay.

Цель: гарантировать, что при ЛЮБОЙ версии FunPayAPI.types.Balance
(старой `total/available` или новой `total_rub/available_rub`) мы
вытаскиваем правильное число в RUB, а не «не распарсил, raw: <...object>».

Так как FunPayAPI обновляется без deprecation-warning'ов, мы делаем
duck-typed подход: пробуем все известные имена полей, берём первое
непустое и нормализуем под ключ "rub" для UI.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_fp_client(account):
    """Конструируем FunPayClient с фейковым account-ом и без _to_thread."""
    from src.funpay.client import FunPayClient

    cli = FunPayClient.__new__(FunPayClient)
    cli._account = account

    async def _to_thread(fn, *a, **kw):
        return fn(*a, **kw)

    cli._to_thread = _to_thread
    return cli


async def test_balance_modern_funpayapi():
    """Новая FunPayAPI: Balance.total_rub / available_rub / total_usd / ..."""
    account = SimpleNamespace(
        id=1,
        get_user=lambda _id: SimpleNamespace(
            get_lots=lambda: [SimpleNamespace(id=12345)]
        ),
        get_balance=lambda _lot: SimpleNamespace(
            total_rub=145.99,
            available_rub=140.0,
            total_usd=1.62,
            available_usd=1.5,
            total_eur=1.45,
            available_eur=1.40,
        ),
    )
    cli = _make_fp_client(account)
    data = await cli.get_funpay_balance()
    assert data.get("error") is None, f"unexpected error: {data}"
    assert data["total_rub"] == 145.99
    # Алиас "rub" должен быть равен total_rub — это ключ, который читает UI
    assert data["rub"] == 145.99
    assert data["usd"] == 1.62


async def test_balance_legacy_funpayapi():
    """Старая FunPayAPI: Balance.total / available / currency."""
    account = SimpleNamespace(
        id=1,
        get_user=lambda _id: SimpleNamespace(
            get_lots=lambda: [SimpleNamespace(id=12345)]
        ),
        get_balance=lambda _lot: SimpleNamespace(
            total=200.0, available=180.0, currency="RUB",
        ),
    )
    cli = _make_fp_client(account)
    data = await cli.get_funpay_balance()
    assert data.get("error") is None
    assert data["total"] == 200.0
    assert data["available"] == 180.0
    assert data["currency"] == "RUB"


async def test_balance_empty_object_falls_back_to_raw_repr():
    """
    Если FunPayAPI снова поменяет имена полей — `error` останется None,
    но в data мы получим только raw_repr. UI покажет «не распарсил, raw:».
    """
    account = SimpleNamespace(
        id=1,
        get_user=lambda _id: SimpleNamespace(
            get_lots=lambda: [SimpleNamespace(id=12345)]
        ),
        get_balance=lambda _lot: SimpleNamespace(),  # пустой объект
    )
    cli = _make_fp_client(account)
    data = await cli.get_funpay_balance()
    # Раз ничего распарсить не смогли — должен быть raw_repr, но НЕ error
    assert data.get("error") is None
    assert "raw_repr" in data
    # И нет ни одного из известных «числовых» полей
    assert all(k not in data for k in (
        "rub", "total", "total_rub", "available", "available_rub",
    ))
