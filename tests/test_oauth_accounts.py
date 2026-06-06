"""
Тесты мульти-аккаунтов (веб-вход без Telegram):
  * миграция старой shop_users (telegram_user_id NOT NULL) → nullable + email/oauth,
    с сохранением данных;
  * get_or_create_oauth_user: поиск по (provider,sub), связка по email, создание.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base
from src.db.session import _migrate_sqlite_schema
from src.shop.repo import get_or_create_oauth_user


OLD_SCHEMA = """
CREATE TABLE shop_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    telegram_username VARCHAR(64),
    first_name VARCHAR(128),
    language_code VARCHAR(8),
    balance_kopecks INTEGER NOT NULL DEFAULT 0,
    referred_by_user_id INTEGER,
    blocked BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME,
    last_seen_at DATETIME
)
"""


async def test_migration_makes_tg_nullable_and_keeps_data():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(OLD_SCHEMA)
        await conn.exec_driver_sql(
            "INSERT INTO shop_users "
            "(telegram_user_id, balance_kopecks, first_name, created_at, last_seen_at) "
            "VALUES (555, 1234, 'Old User', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        await conn.run_sync(_migrate_sqlite_schema)

    async with engine.begin() as conn:
        cols = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("shop_users")}
        )
        assert {"email", "auth_provider", "oauth_sub"} <= cols
        # данные сохранены
        res = await conn.exec_driver_sql(
            "SELECT first_name, balance_kopecks FROM shop_users WHERE telegram_user_id=555"
        )
        row = res.fetchone()
        assert row[0] == "Old User" and row[1] == 1234
        # telegram_user_id теперь nullable
        await conn.exec_driver_sql(
            "INSERT INTO shop_users "
            "(telegram_user_id, balance_kopecks, blocked, email, auth_provider, oauth_sub) "
            "VALUES (NULL, 0, 0, 'x@y.z', 'google', 'sub1')"
        )
    await engine.dispose()


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


async def test_oauth_find_by_provider_sub(factory):
    async with factory() as s:
        u1, new1 = await get_or_create_oauth_user(
            s, provider="google", sub="abc", email="a@b.c", first_name="A",
        )
        await s.commit()
    assert new1 and u1.telegram_user_id is None
    async with factory() as s:
        u2, new2 = await get_or_create_oauth_user(s, provider="google", sub="abc")
    assert not new2 and u2.id == u1.id


async def test_oauth_links_by_email(factory):
    async with factory() as s:
        u1, _ = await get_or_create_oauth_user(
            s, provider="google", sub="abc", email="same@mail.ru",
        )
        await s.commit()
    async with factory() as s:
        u3, new3 = await get_or_create_oauth_user(
            s, provider="yandex", sub="zzz", email="same@mail.ru",
        )
        await s.commit()
    assert not new3 and u3.id == u1.id


async def test_oauth_creates_distinct(factory):
    async with factory() as s:
        u1, _ = await get_or_create_oauth_user(s, provider="google", sub="a")
        u2, new2 = await get_or_create_oauth_user(s, provider="google", sub="b")
        await s.commit()
    assert new2 and u1.id != u2.id
