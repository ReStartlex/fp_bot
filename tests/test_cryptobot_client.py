"""
Тесты CryptoBotClient: моки httpx, проверка request/response сценариев.

Не идём в реальную сеть. Используем pytest-asyncio + httpx.MockTransport
для перехвата запросов.
"""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from src.shop.payments.cryptobot import (
    CRYPTOBOT_MAINNET_URL,
    CRYPTOBOT_TESTNET_URL,
    CryptoBotClient,
    CryptoBotError,
    Invoice,
)


# ─── Invoice.from_api ───────────────────────────────────────────────


def test_invoice_from_api_full_payload():
    inv = Invoice.from_api({
        "invoice_id": 12345,
        "status": "active",
        "amount": "500.00",
        "fiat": "RUB",
        "bot_invoice_url": "https://t.me/CryptoBot?start=I_abc",
        "description": "Пополнение",
        "payload": "payment_id:42",
        "created_at": "2026-05-25T18:00:00.000Z",
    })
    assert inv.invoice_id == 12345
    assert inv.status == "active"
    assert inv.amount == Decimal("500.00")
    assert inv.fiat == "RUB"
    assert "CryptoBot" in inv.pay_url
    assert inv.payload == "payment_id:42"
    assert inv.paid_at is None


def test_invoice_from_api_falls_back_to_pay_url_legacy_field():
    inv = Invoice.from_api({
        "invoice_id": 1,
        "status": "paid",
        "amount": "100",
        "pay_url": "https://example.com/pay",
        "asset": "USDT",
    })
    assert inv.pay_url == "https://example.com/pay"
    # 'asset' принимается как fallback для fiat если fiat не задан
    assert inv.fiat == "USDT"


def test_invoice_from_api_keeps_raw_for_audit():
    raw = {"invoice_id": 1, "status": "paid", "amount": "1", "x": "y"}
    inv = Invoice.from_api(raw)
    assert inv.raw == raw


# ─── CryptoBotClient init ───────────────────────────────────────────


def test_client_requires_token():
    with pytest.raises(ValueError):
        CryptoBotClient(api_token="")


def test_client_mainnet_by_default():
    cli = CryptoBotClient(api_token="abc")
    assert cli.base_url == CRYPTOBOT_MAINNET_URL


def test_client_testnet_url_when_enabled():
    cli = CryptoBotClient(api_token="abc", testnet=True)
    assert cli.base_url == CRYPTOBOT_TESTNET_URL


# ─── createInvoice happy path ───────────────────────────────────────


class _MockTransport:
    """
    Лёгкий мок httpx.AsyncClient.post через monkeypatch'инг
    httpx.AsyncClient на наш _FakeAsyncClient.
    """

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict, dict]] = []  # (url, json, headers)

    def __call__(self, *args, **kwargs):
        responder, calls = self.responder, self.calls

        class _FakeAsyncClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def post(self, url, *, json=None, headers=None):
                calls.append((url, json, headers))
                return responder(url, json or {}, headers or {})

        return _FakeAsyncClient()


def _resp(status_code: int, body):
    return httpx.Response(
        status_code,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )


async def test_create_invoice_returns_parsed_invoice(monkeypatch):
    def responder(url, body, headers):
        # Telegram передаёт api-token в заголовке
        assert headers["Crypto-Pay-API-Token"] == "abc"
        assert url.endswith("/createInvoice")
        # Тело — fiat=RUB, amount, payload
        assert body["currency_type"] == "fiat"
        assert body["fiat"] == "RUB"
        assert body["amount"] == "500"
        assert body["payload"] == "payment_id:7"
        return _resp(200, {
            "ok": True,
            "result": {
                "invoice_id": 999,
                "status": "active",
                "amount": "500",
                "fiat": "RUB",
                "bot_invoice_url": "https://t.me/CryptoBot?start=I_777",
                "payload": "payment_id:7",
            },
        })

    monkeypatch.setattr("httpx.AsyncClient", _MockTransport(responder))
    cli = CryptoBotClient(api_token="abc")
    inv = await cli.create_invoice(
        amount_rub=Decimal("500"),
        description="test",
        payload="payment_id:7",
    )
    assert inv.invoice_id == 999
    assert inv.status == "active"
    assert inv.payload == "payment_id:7"
    assert "CryptoBot" in inv.pay_url


async def test_create_invoice_rejects_zero_amount():
    cli = CryptoBotClient(api_token="abc")
    with pytest.raises(ValueError):
        await cli.create_invoice(
            amount_rub=Decimal(0), description="x", payload="x",
        )


async def test_create_invoice_rejects_negative_amount():
    cli = CryptoBotClient(api_token="abc")
    with pytest.raises(ValueError):
        await cli.create_invoice(
            amount_rub=Decimal("-10"), description="x", payload="x",
        )


# ─── API errors ─────────────────────────────────────────────────────


async def test_api_error_raised(monkeypatch):
    def responder(*a, **kw):
        return _resp(200, {
            "ok": False,
            "error": {"code": 401, "name": "UNAUTHORIZED"},
        })
    monkeypatch.setattr("httpx.AsyncClient", _MockTransport(responder))
    cli = CryptoBotClient(api_token="bad")
    with pytest.raises(CryptoBotError) as exc:
        await cli.get_me()
    assert exc.value.code == 401
    assert "UNAUTHORIZED" in exc.value.name


async def test_5xx_raises_server_error(monkeypatch):
    def responder(*a, **kw):
        return _resp(503, {})
    monkeypatch.setattr("httpx.AsyncClient", _MockTransport(responder))
    cli = CryptoBotClient(api_token="abc")
    with pytest.raises(CryptoBotError) as exc:
        await cli.get_me()
    assert exc.value.code == 503


# ─── getInvoices ────────────────────────────────────────────────────


async def test_get_invoices_filters_status(monkeypatch):
    captured: dict = {}

    def responder(url, body, headers):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {
            "ok": True,
            "result": {"items": [
                {"invoice_id": 1, "status": "paid", "amount": "100",
                 "fiat": "RUB", "bot_invoice_url": "x"},
                {"invoice_id": 2, "status": "paid", "amount": "200",
                 "fiat": "RUB", "bot_invoice_url": "y"},
            ]},
        })

    monkeypatch.setattr("httpx.AsyncClient", _MockTransport(responder))
    cli = CryptoBotClient(api_token="abc")
    items = await cli.get_invoices(status="paid")
    assert captured["body"]["status"] == "paid"
    assert captured["url"].endswith("/getInvoices")
    assert len(items) == 2
    assert all(i.status == "paid" for i in items)


async def test_get_invoice_returns_none_when_not_found(monkeypatch):
    def responder(*a, **kw):
        return _resp(200, {"ok": True, "result": {"items": []}})
    monkeypatch.setattr("httpx.AsyncClient", _MockTransport(responder))
    cli = CryptoBotClient(api_token="abc")
    assert await cli.get_invoice(99999) is None
