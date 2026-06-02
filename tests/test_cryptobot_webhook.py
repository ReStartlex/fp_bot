"""
Тесты webhook-endpoint'а CryptoBot.

Используем httpx.AsyncClient + ASGI напрямую, без сети.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from src.db.models import Base
from src.shop.repo import (
    create_topup_payment,
    get_balance_stats,
    get_or_create_user,
    get_payment,
)


def _sig(api_token: str, body: bytes) -> str:
    secret = hashlib.sha256(api_token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture()
async def webhook_app(monkeypatch, tmp_path):
    """
    Поднимаем FastAPI app + in-memory SQLite + cryptobot_api_token.
    """
    import src.db.session as session_mod
    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    saved_engine = session_mod._engine
    saved_factory = session_mod._session_factory
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_mod._engine = engine
    session_mod._session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession,
    )

    # Создаём mini-app только с нашим router'ом (не зовём create_app(),
    # потому что lifespan там делает init_db с реальной БД).
    from fastapi import FastAPI
    from src.api.cryptobot_webhook import router

    app = FastAPI()
    app.include_router(router)

    # Подмешиваем токен в Settings
    from src.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(
        settings, "__class__",
        type(settings.__class__.__name__, (settings.__class__,), {}),
        raising=False,
    )
    # Простое решение: monkey-patch объекта через __dict__
    object.__setattr__(settings, "cryptobot_api_token", SecretStr("tt"))

    yield app, "tt"

    await engine.dispose()
    session_mod._engine = saved_engine
    session_mod._session_factory = saved_factory


async def test_webhook_rejects_bad_signature(webhook_app):
    app, token = webhook_app
    body = json.dumps({"update_type": "invoice_paid", "payload": {}}).encode()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as cli:
        r = await cli.post(
            "/api/cryptobot/webhook",
            content=body,
            headers={"crypto-pay-api-signature": "deadbeef"},
        )
    assert r.status_code == 401


async def test_webhook_rejects_missing_signature(webhook_app):
    app, _ = webhook_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as cli:
        r = await cli.post("/api/cryptobot/webhook", json={"a": "b"})
    assert r.status_code == 401


async def test_webhook_credits_balance_on_paid(webhook_app):
    app, token = webhook_app
    # Создать pending payment
    from src.db.session import session_factory
    async with session_factory()() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=10)
        u_id = u.id
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="555",
            amount_kopecks=50000,
            notify_telegram_id=10,
        )
        await s.commit()

    payload = {
        "update_type": "invoice_paid",
        "payload": {
            "invoice_id": 555,
            "amount": "500",
            "fiat": "RUB",
            "status": "paid",
        },
    }
    body = json.dumps(payload).encode()
    sig = _sig(token, body)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as cli:
        r = await cli.post(
            "/api/cryptobot/webhook",
            content=body,
            headers={
                "crypto-pay-api-signature": sig,
                "content-type": "application/json",
            },
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "applied": True}

    async with session_factory()() as s:
        stats = await get_balance_stats(s, user_id=u_id)
        assert stats.current_kopecks == 50000


async def test_webhook_idempotent_on_repeated_paid(webhook_app):
    app, token = webhook_app
    from src.db.session import session_factory
    async with session_factory()() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=20)
        u_id = u.id
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="X",
            amount_kopecks=10000,
        )
        await s.commit()

    payload = {
        "update_type": "invoice_paid",
        "payload": {"invoice_id": "X", "amount": "100", "status": "paid"},
    }
    body = json.dumps(payload).encode()
    sig = _sig(token, body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as cli:
        r1 = await cli.post(
            "/api/cryptobot/webhook", content=body,
            headers={"crypto-pay-api-signature": sig},
        )
        r2 = await cli.post(
            "/api/cryptobot/webhook", content=body,
            headers={"crypto-pay-api-signature": sig},
        )
    assert r1.json()["applied"] is True
    assert r2.json()["applied"] is False  # idempotent
    async with session_factory()() as s:
        stats = await get_balance_stats(s, user_id=u_id)
        assert stats.current_kopecks == 10000  # NOT 20000


async def test_webhook_invoice_expired_marks_failed(webhook_app):
    app, token = webhook_app
    from src.db.session import session_factory
    async with session_factory()() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=30)
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="EX", amount_kopecks=10000,
        )
        await s.commit()

    payload = {
        "update_type": "invoice_expired",
        "payload": {"invoice_id": "EX"},
    }
    body = json.dumps(payload).encode()
    sig = _sig(token, body)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as cli:
        r = await cli.post(
            "/api/cryptobot/webhook", content=body,
            headers={"crypto-pay-api-signature": sig},
        )
    assert r.status_code == 200
    async with session_factory()() as s:
        p = await get_payment(s, provider="cryptobot", provider_invoice_id="EX")
        assert p.status == "failed"
        assert p.error == "expired"


async def test_webhook_ignores_unknown_update_type(webhook_app):
    app, token = webhook_app
    payload = {"update_type": "foo_bar", "payload": {}}
    body = json.dumps(payload).encode()
    sig = _sig(token, body)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as cli:
        r = await cli.post(
            "/api/cryptobot/webhook", content=body,
            headers={"crypto-pay-api-signature": sig},
        )
    assert r.status_code == 200
    assert r.json().get("ignored") is True
