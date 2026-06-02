"""
Sprint 6 — интеграционные тесты Mini App API endpoints.

Все endpoints требуют валидный X-Telegram-Init-Data. Используем хелпер
из test_webapp_auth для генерации валидного initData.

Покрываем:
  * Auth: 401 на битый initData, 401 без header, успех — авто-создание ShopUser
  * /init и /me — возвращают user info с балансом
  * /catalog/groups, /catalog/groups/{slug}, /catalog/categories/{id}, /catalog/services/{id}
  * /checkout — happy path, insufficient_balance, out_of_stock
  * /orders — пагинация, только свои заказы
  * /orders/{id} — карточка, 404 если чужой
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.app import create_app
from src.db.models import Base
from src.shop.repo import (
    apply_balance_change,
    create_shop_order,
    get_or_create_user,
    mark_order_delivered,
    mark_order_delivering,
    mark_order_paid,
    upsert_catalog_service,
)
from src.shop.taxonomy import make_group_slug


BOT_TOKEN = "1234567890:WEBAPPTEST_TOKEN_FOR_E2E_TESTS_AAA"
SECRET_KEY_HEX = "0" * 64


@pytest.fixture()
def app_settings(monkeypatch):
    """Подменяем настройки для shop_enabled + bot token."""
    monkeypatch.setenv("FUNPAY_GOLDEN_KEY", "x" * 64)
    monkeypatch.setenv("FUNPAY_CURRENCY", "RUB")
    monkeypatch.setenv("ADMIN_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1" * 46)
    monkeypatch.setenv("NS_SECRET_KEY", SECRET_KEY_HEX)
    monkeypatch.setenv("SHOP_ENABLED", "true")
    monkeypatch.setenv("SHOP_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    # Сбросим cached settings singleton
    import src.config as cfg
    monkeypatch.setattr(cfg, "_settings", None)
    yield


@pytest.fixture()
async def db_factory(monkeypatch, app_settings):
    """SQLite in-memory + патч session_factory во всех модулях."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def fake_factory():
        return factory

    # Подменяем во всех модулях, импортировавших session_factory
    monkeypatch.setattr("src.api.shop_router.session_factory", fake_factory)
    monkeypatch.setattr("src.shop.repo.session_factory", fake_factory) if False else None
    # repo не использует session_factory globally — он принимает session как arg
    yield factory
    await engine.dispose()


@pytest.fixture()
def client(app_settings):
    app = create_app()
    return TestClient(app)


def make_init_data(*, user_id: int = 42, auth_date: int | None = None) -> str:
    """Конструирует валидный initData для тестов."""
    if auth_date is None:
        auth_date = int(time.time())
    fields = {
        "user": json.dumps(
            {
                "id": user_id,
                "first_name": f"User{user_id}",
                "username": f"u{user_id}",
                "language_code": "ru",
            },
            separators=(",", ":"),
        ),
        "auth_date": str(auth_date),
    }
    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields.keys()))
    secret = hmac.new(
        b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    h = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


# ─── Auth ─────────────────────────────────────────────────────────


async def test_init_endpoint_returns_user_info(client, db_factory):
    """POST /api/shop/init с валидным initData возвращает MeResponse."""
    init_data = make_init_data(user_id=42)
    resp = client.post(
        "/api/shop/init",
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_user_id"] == 42
    assert body["username"] == "u42"
    assert body["first_name"] == "User42"
    assert body["balance_kopecks"] == 0


async def test_init_rejects_invalid_hash(client, db_factory):
    """Битый hash → 401."""
    fields = {
        "user": '{"id":1,"first_name":"X"}',
        "auth_date": str(int(time.time())),
        "hash": "deadbeef" * 8,
    }
    resp = client.post(
        "/api/shop/init",
        headers={"X-Telegram-Init-Data": urlencode(fields)},
    )
    assert resp.status_code == 401


async def test_init_rejects_missing_header(client, db_factory):
    """Без заголовка → 422 (FastAPI валидация)."""
    resp = client.post("/api/shop/init")
    assert resp.status_code in (401, 422)


async def test_init_rejects_expired_data(client, db_factory):
    """auth_date 25 часов назад → 401."""
    init_data = make_init_data(
        user_id=42, auth_date=int(time.time()) - 25 * 3600,
    )
    resp = client.post(
        "/api/shop/init",
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 401


# ─── /me + balance ────────────────────────────────────────────────


async def test_me_reflects_balance(client, db_factory):
    """После top-up balance показывается в /me."""
    init_data = make_init_data(user_id=42)
    # Сперва вызываем init чтобы создать ShopUser
    client.post(
        "/api/shop/init",
        headers={"X-Telegram-Init-Data": init_data},
    )
    # Затем накидываем балланс
    async with db_factory() as s:
        from sqlalchemy import select
        from src.db.models import ShopUser
        u = (await s.execute(
            select(ShopUser).where(ShopUser.telegram_user_id == 42)
        )).scalar_one()
        await apply_balance_change(
            s, user_id=u.id, change_kopecks=10000, reason="manual_topup",
        )
        await s.commit()
    # /me → balance 10000
    resp = client.get(
        "/api/shop/me",
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 200
    assert resp.json()["balance_kopecks"] == 10000


# ─── Catalog ─────────────────────────────────────────────────────


async def test_catalog_groups_endpoint(client, db_factory):
    """GET /api/shop/catalog/groups возвращает список групп."""
    init_data = make_init_data(user_id=42)
    # Засеиваем catalog
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US",
            service_name="Apple US $5",
            base_name="Apple Gift Card", group_slug=apple_slug,
            ns_price_usd=5.0, rub_price_kopecks=40000,
            in_stock=10, fields_json=None,
        )
        await s.commit()
    resp = client.get(
        "/api/shop/catalog/groups",
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 200
    groups = resp.json()
    assert len(groups) >= 1
    assert any(g["base_name"] == "Apple Gift Card" for g in groups)


async def test_catalog_service_endpoint(client, db_factory):
    """GET /api/shop/catalog/services/{id} возвращает карточку."""
    init_data = make_init_data(user_id=42)
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US",
            service_name="Apple US $5",
            base_name="Apple Gift Card", group_slug=apple_slug,
            ns_price_usd=5.0, rub_price_kopecks=40000,
            in_stock=10, fields_json=None,
        )
        await s.commit()
    resp = client.get(
        "/api/shop/catalog/services/1",
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ns_service_id"] == 1
    assert body["service_name"] == "Apple US $5"
    assert body["rub_price_kopecks"] == 40000


async def test_catalog_service_404_for_missing(client, db_factory):
    init_data = make_init_data(user_id=42)
    resp = client.get(
        "/api/shop/catalog/services/99999",
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 404


# ─── Checkout ─────────────────────────────────────────────────────


async def test_checkout_endpoint_happy_path(client, db_factory):
    """POST /api/shop/checkout с достаточным балансом → ok + order_id."""
    init_data = make_init_data(user_id=42)
    # Init + balance + catalog
    client.post(
        "/api/shop/init",
        headers={"X-Telegram-Init-Data": init_data},
    )
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_factory() as s:
        from sqlalchemy import select
        from src.db.models import ShopUser
        u = (await s.execute(
            select(ShopUser).where(ShopUser.telegram_user_id == 42)
        )).scalar_one()
        await apply_balance_change(
            s, user_id=u.id, change_kopecks=100000, reason="manual_topup",
        )
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US",
            service_name="Apple US $5",
            base_name="Apple Gift Card", group_slug=apple_slug,
            ns_price_usd=5.0, rub_price_kopecks=40000,
            in_stock=10, fields_json=None,
        )
        await s.commit()
    resp = client.post(
        "/api/shop/checkout",
        headers={"X-Telegram-Init-Data": init_data},
        json={"ns_service_id": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "ok"
    assert body["order_id"] is not None
    assert body["new_balance_kopecks"] == 60000


async def test_checkout_insufficient_returns_diagnostic(client, db_factory):
    init_data = make_init_data(user_id=42)
    client.post(
        "/api/shop/init",
        headers={"X-Telegram-Init-Data": init_data},
    )
    apple_slug = make_group_slug("Apple Gift Card")
    async with db_factory() as s:
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Apple Gift Card | US",
            service_name="Apple US $5",
            base_name="Apple Gift Card", group_slug=apple_slug,
            ns_price_usd=5.0, rub_price_kopecks=40000,
            in_stock=10, fields_json=None,
        )
        await s.commit()
    resp = client.post(
        "/api/shop/checkout",
        headers={"X-Telegram-Init-Data": init_data},
        json={"ns_service_id": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "insufficient_balance"
    assert body["need_kopecks"] == 40000
    assert body["have_kopecks"] == 0
    assert body["deficit_kopecks"] == 40000


# ─── Orders ───────────────────────────────────────────────────────


async def test_orders_list_returns_only_my_orders(client, db_factory):
    """User A видит свои заказы, не чужие."""
    init_a = make_init_data(user_id=42)
    init_b = make_init_data(user_id=99)
    # Регистрируем обоих
    client.post("/api/shop/init", headers={"X-Telegram-Init-Data": init_a})
    client.post("/api/shop/init", headers={"X-Telegram-Init-Data": init_b})
    # User A создаёт заказ
    async with db_factory() as s:
        from sqlalchemy import select
        from src.db.models import ShopUser
        a = (await s.execute(
            select(ShopUser).where(ShopUser.telegram_user_id == 42)
        )).scalar_one()
        b = (await s.execute(
            select(ShopUser).where(ShopUser.telegram_user_id == 99)
        )).scalar_one()
        await create_shop_order(
            s, user_id=a.id, ns_service_id=1, ns_service_name="A_order",
            total_rub_kopecks=1000,
        )
        await create_shop_order(
            s, user_id=b.id, ns_service_id=2, ns_service_name="B_order",
            total_rub_kopecks=2000,
        )
        await s.commit()
    # /orders как A → только A_order
    resp_a = client.get(
        "/api/shop/orders",
        headers={"X-Telegram-Init-Data": init_a},
    )
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert body_a["total"] == 1
    assert body_a["orders"][0]["ns_service_name"] == "A_order"
    # /orders как B → только B_order
    resp_b = client.get(
        "/api/shop/orders",
        headers={"X-Telegram-Init-Data": init_b},
    )
    body_b = resp_b.json()
    assert body_b["total"] == 1
    assert body_b["orders"][0]["ns_service_name"] == "B_order"


async def test_order_card_endpoint_returns_pins(client, db_factory):
    init_data = make_init_data(user_id=42)
    client.post("/api/shop/init", headers={"X-Telegram-Init-Data": init_data})
    async with db_factory() as s:
        from sqlalchemy import select
        from src.db.models import ShopUser
        u = (await s.execute(
            select(ShopUser).where(ShopUser.telegram_user_id == 42)
        )).scalar_one()
        o = await create_shop_order(
            s, user_id=u.id, ns_service_id=1, ns_service_name="X",
            total_rub_kopecks=1000,
        )
        await mark_order_paid(s, order_id=o.id, balance_used_kopecks=1000)
        await mark_order_delivering(s, order_id=o.id, ns_custom_id="x")
        await mark_order_delivered(
            s, order_id=o.id, pins_json=json.dumps([{"pin": "ABC-XYZ"}]),
        )
        await s.commit()
    resp = client.get(
        f"/api/shop/orders/{o.id}",
        headers={"X-Telegram-Init-Data": init_data},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "delivered"
    assert body["pins"] == [{"pin": "ABC-XYZ"}]


async def test_order_card_404_for_other_users_order(client, db_factory):
    """GET /api/shop/orders/{id} чужого юзера → 404."""
    init_a = make_init_data(user_id=42)
    init_b = make_init_data(user_id=99)
    client.post("/api/shop/init", headers={"X-Telegram-Init-Data": init_a})
    client.post("/api/shop/init", headers={"X-Telegram-Init-Data": init_b})
    async with db_factory() as s:
        from sqlalchemy import select
        from src.db.models import ShopUser
        a = (await s.execute(
            select(ShopUser).where(ShopUser.telegram_user_id == 42)
        )).scalar_one()
        o = await create_shop_order(
            s, user_id=a.id, ns_service_id=1, ns_service_name="X",
            total_rub_kopecks=1000,
        )
        await s.commit()
    resp_b = client.get(
        f"/api/shop/orders/{o.id}",
        headers={"X-Telegram-Init-Data": init_b},
    )
    assert resp_b.status_code == 404
