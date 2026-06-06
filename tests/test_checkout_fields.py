"""
Тесты сбора и валидации доп. полей при checkout (товары с обязательными
полями вроде ID игрока). Регрессия: раньше такие товары вообще нельзя
было купить (REQUIRES_FIELDS — заглушка).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base
from src.shop.checkout import (
    CheckoutOutcome,
    attempt_checkout_via_balance,
    validate_and_build_fields,
)
from src.shop.repo import (
    apply_balance_change,
    get_or_create_user,
    upsert_catalog_service,
)


SCHEMA = [
    {"key": "player_id", "type": "string", "name": "ID игрока", "required": True},
    {"key": "server", "type": "string", "name": "Сервер", "required": True,
     "enum": ["EU", "NA"]},
]


@pytest.fixture()
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ─── unit: validate_and_build_fields ───────────────────────────────


def test_validate_missing_required():
    built, err = validate_and_build_fields(SCHEMA, {"server": "EU"})
    assert built is None
    assert "ID игрока" in err


def test_validate_enum_rejected():
    built, err = validate_and_build_fields(
        SCHEMA, {"player_id": "123", "server": "ASIA"},
    )
    assert built is None
    assert "Сервер" in err


def test_validate_ok_builds_key_value():
    built, err = validate_and_build_fields(
        SCHEMA, {"player_id": "123", "server": "EU"},
    )
    assert err is None
    assert {"key": "player_id", "value": "123"} in built
    assert {"key": "server", "value": "EU"} in built


def test_validate_numeric_min_max():
    schema = [{"key": "amount", "type": "int", "name": "Кол-во",
               "required": True, "min": 1, "max": 10}]
    _, err = validate_and_build_fields(schema, {"amount": "20"})
    assert "максимум" in err
    built, err2 = validate_and_build_fields(schema, {"amount": "5"})
    assert err2 is None
    assert built == [{"key": "amount", "value": 5}]


# ─── integration: checkout with fields ─────────────────────────────


async def _seed(factory, *, balance=100000, stock=10):
    async with factory() as s:
        user, _ = await get_or_create_user(s, telegram_user_id=555)
        await apply_balance_change(
            s, user_id=user.id, change_kopecks=balance, reason="manual_topup",
        )
        await upsert_catalog_service(
            s, ns_service_id=1, category_id=10,
            category_name="Game | Topup", service_name="Game 60 Silver",
            base_name="Game", group_slug="game",
            ns_price_usd=1.0, rub_price_kopecks=7727, in_stock=stock,
            fields_json=json.dumps(SCHEMA, ensure_ascii=False),
        )
        await s.commit()
        return user.id


async def test_checkout_without_fields_returns_schema(db_factory):
    uid = await _seed(db_factory)
    async with db_factory() as s:
        res = await attempt_checkout_via_balance(s, user_id=uid, ns_service_id=1)
        await s.rollback()
    assert res.outcome == CheckoutOutcome.REQUIRES_FIELDS
    assert res.required_fields and len(res.required_fields) == 2


async def test_checkout_invalid_fields_returns_error(db_factory):
    uid = await _seed(db_factory)
    async with db_factory() as s:
        res = await attempt_checkout_via_balance(
            s, user_id=uid, ns_service_id=1,
            field_values={"player_id": "", "server": "EU"},
        )
        await s.rollback()
    assert res.outcome == CheckoutOutcome.REQUIRES_FIELDS
    assert res.field_error is not None


async def test_checkout_with_valid_fields_succeeds(db_factory):
    uid = await _seed(db_factory)
    async with db_factory() as s:
        res = await attempt_checkout_via_balance(
            s, user_id=uid, ns_service_id=1,
            field_values={"player_id": "777", "server": "NA"},
        )
        if res.outcome == CheckoutOutcome.OK:
            await s.commit()
    assert res.outcome == CheckoutOutcome.OK
    stored = json.loads(res.order.fields_json)
    assert {"key": "player_id", "value": "777"} in stored
    assert {"key": "server", "value": "NA"} in stored
