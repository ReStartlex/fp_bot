"""
Интеграционные тесты авторизованного API сайта (`/api/site/*`).

Логин через подписанный Login Widget payload, дальше — кабинет под
cookie/Bearer-сессией. SITE_COOKIE_SECURE=false, чтобы TestClient (http)
пересылал cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.app import create_app
from src.db.models import Base
from src.shop.repo import (
    apply_balance_change,
    create_shop_order,
    get_user_by_tg,
    upsert_catalog_service,
)
from src.shop.taxonomy import make_group_slug


BOT_TOKEN = "123456:SITE_ROUTER_TEST_TOKEN"


@pytest.fixture()
def app_settings(monkeypatch):
    monkeypatch.setenv("FUNPAY_GOLDEN_KEY", "x" * 64)
    monkeypatch.setenv("FUNPAY_CURRENCY", "RUB")
    monkeypatch.setenv("SHOP_ENABLED", "true")
    monkeypatch.setenv("SHOP_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("SITE_COOKIE_SECURE", "false")
    monkeypatch.setenv("CRYPTOBOT_API_TOKEN", "123:CRYPTOTEST")
    import src.config as cfg
    monkeypatch.setattr(cfg, "_settings", None)
    yield


@pytest.fixture()
async def db_factory(monkeypatch, app_settings):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def fake_factory():
        return factory

    monkeypatch.setattr("src.api.site_router.session_factory", fake_factory)
    yield factory
    await engine.dispose()


@pytest.fixture()
def client(app_settings):
    return TestClient(create_app())


def make_login(user_id: int = 555, auth_date: int | None = None) -> dict:
    if auth_date is None:
        auth_date = int(time.time())
    data = {
        "id": str(user_id),
        "first_name": f"User{user_id}",
        "username": f"u{user_id}",
        "auth_date": str(auth_date),
    }
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return data


# ─── Login ─────────────────────────────────────────────────────────


async def test_login_creates_user_and_sets_cookie(client, db_factory):
    resp = client.post("/api/site/auth/telegram", json=make_login(555))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_user_id"] == 555
    assert body["balance_kopecks"] == 0
    assert body["token"]
    assert "nd_session" in resp.cookies
    # юзер реально создан
    async with db_factory() as s:
        u = await get_user_by_tg(s, telegram_user_id=555)
        assert u is not None


async def test_login_rejects_bad_hash(client, db_factory):
    data = make_login(1)
    data["hash"] = "00" * 32
    resp = client.post("/api/site/auth/telegram", json=data)
    assert resp.status_code == 401


# ─── Session: cookie & bearer ──────────────────────────────────────


async def test_me_via_cookie(client, db_factory):
    client.post("/api/site/auth/telegram", json=make_login(555))
    resp = client.get("/api/site/me")  # cookie уже в client
    assert resp.status_code == 200
    assert resp.json()["telegram_user_id"] == 555


async def test_me_via_bearer(client, db_factory):
    login = client.post("/api/site/auth/telegram", json=make_login(555))
    token = login.json()["token"]
    # Чистый клиент без cookie — только заголовок
    fresh = TestClient(client.app)
    resp = fresh.get("/api/site/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["telegram_user_id"] == 555


async def test_me_without_auth_401(client, db_factory):
    resp = client.get("/api/site/me")
    assert resp.status_code == 401


async def test_logout_clears_session(client, db_factory):
    client.post("/api/site/auth/telegram", json=make_login(555))
    client.post("/api/site/auth/logout")
    client.cookies.clear()
    resp = client.get("/api/site/me")
    assert resp.status_code == 401


# ─── Orders & checkout ─────────────────────────────────────────────


async def test_orders_only_mine(client, db_factory):
    client.post("/api/site/auth/telegram", json=make_login(555))
    async with db_factory() as s:
        me = await get_user_by_tg(s, telegram_user_id=555)
        await create_shop_order(
            s, user_id=me.id, ns_service_id=1, ns_service_name="Mine",
            total_rub_kopecks=1000,
        )
        # чужой заказ
        from src.shop.repo import get_or_create_user
        other, _ = await get_or_create_user(s, telegram_user_id=999)
        await create_shop_order(
            s, user_id=other.id, ns_service_id=2, ns_service_name="Theirs",
            total_rub_kopecks=2000,
        )
        await s.commit()
    resp = client.get("/api/site/orders")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["orders"][0]["ns_service_name"] == "Mine"


async def test_checkout_happy_path(client, db_factory):
    client.post("/api/site/auth/telegram", json=make_login(555))
    slug = make_group_slug("Apple Gift Card")
    async with db_factory() as s:
        me = await get_user_by_tg(s, telegram_user_id=555)
        await apply_balance_change(
            s, user_id=me.id, change_kopecks=100000, reason="manual_topup",
        )
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US", service_name="Apple US $5",
            base_name="Apple Gift Card", group_slug=slug,
            ns_price_usd=5.0, rub_price_kopecks=40000, in_stock=10,
            fields_json=None,
        )
        await s.commit()
    resp = client.post("/api/site/checkout", json={"ns_service_id": 1})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "ok"
    assert body["order_id"] is not None
    assert body["new_balance_kopecks"] == 60000


async def test_checkout_insufficient(client, db_factory):
    client.post("/api/site/auth/telegram", json=make_login(555))
    slug = make_group_slug("Apple Gift Card")
    async with db_factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US", service_name="Apple US $5",
            base_name="Apple Gift Card", group_slug=slug,
            ns_price_usd=5.0, rub_price_kopecks=40000, in_stock=10,
            fields_json=None,
        )
        await s.commit()
    resp = client.post("/api/site/checkout", json={"ns_service_id": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "insufficient_balance"
    assert body["deficit_kopecks"] == 40000


async def test_checkout_requires_auth(client, db_factory):
    resp = client.post("/api/site/checkout", json={"ns_service_id": 1})
    assert resp.status_code == 401


# ─── Topup (CryptoBot) ─────────────────────────────────────────────


class _FakeInvoice:
    invoice_id = 777
    pay_url = "https://t.me/CryptoBot?start=inv777"


class _FakeCryptoClient:
    def __init__(self, **kwargs):
        pass

    async def create_invoice(self, **kwargs):
        return _FakeInvoice()


async def test_topup_creates_invoice(client, db_factory, monkeypatch):
    monkeypatch.setattr("src.api.site_router.CryptoBotClient", _FakeCryptoClient)
    client.post("/api/site/auth/telegram", json=make_login(555))
    resp = client.post("/api/site/topup", json={"amount_rub": 500})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pay_url"].startswith("https://t.me/CryptoBot")
    assert body["amount_kopecks"] == 50000
    # ShopPayment(pending) создан
    async with db_factory() as s:
        from sqlalchemy import select
        from src.db.models import ShopPayment
        rows = (await s.execute(select(ShopPayment))).scalars().all()
        assert len(rows) == 1
        assert rows[0].provider == "cryptobot"
        assert rows[0].amount_kopecks == 50000


async def test_topup_requires_auth(client, db_factory):
    resp = client.post("/api/site/topup", json={"amount_rub": 500})
    assert resp.status_code == 401


async def test_topup_below_min_rejected(client, db_factory, monkeypatch):
    monkeypatch.setattr("src.api.site_router.CryptoBotClient", _FakeCryptoClient)
    client.post("/api/site/auth/telegram", json=make_login(555))
    resp = client.post("/api/site/topup", json={"amount_rub": 1})
    assert resp.status_code == 422
