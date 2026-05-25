"""
Тесты inline-query handler'а shop-бота (`@MyShopBot apple` в любом чате).

Mock'аем InlineQuery (q.answer = AsyncMock), проверяем:
- query <2 символов → пустой ответ;
- query «apple» → список InlineQueryResultArticle;
- учитываются OOS-услуги (исключаются);
- title/description заполнены и в пределах Telegram-лимитов.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base
from src.shop.bot import ShopBot
from src.shop.repo import upsert_catalog_service
from src.shop.taxonomy import make_group_slug


def _settings():
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x", ns_api_secret="QQ==",
        funpay_golden_key="x", funpay_user_id=1,
        shop_enabled=True, shop_telegram_bot_token="dummy",
    )


@pytest.fixture()
async def db_setup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.shop.bot.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _fake_inline_query(text: str):
    q = SimpleNamespace()
    q.query = text
    q.from_user = SimpleNamespace(id=1)
    q.answer = AsyncMock()
    return q


async def _seed(factory):
    apple_slug = make_group_slug("Apple")
    spo_slug = make_group_slug("Spotify")
    async with factory() as s:
        for sid, name, price in [
            (1, "Apple $5", 40000),
            (2, "Apple $10", 80000),
        ]:
            await upsert_catalog_service(
                s, ns_service_id=sid, category_id=10, category_name="Apple",
                service_name=name, ns_price_usd=5,
                rub_price_kopecks=price, in_stock=10, fields_json=None,
                base_name="Apple", group_slug=apple_slug,
            )
        await upsert_catalog_service(
            s, ns_service_id=9, category_id=30, category_name="Spotify",
            service_name="Spotify Premium", ns_price_usd=5,
            rub_price_kopecks=40000, in_stock=0, fields_json=None,
            base_name="Spotify", group_slug=spo_slug,
        )
        await s.commit()


async def test_inline_short_query_returns_empty(db_setup):
    await _seed(db_setup)
    bot = ShopBot(_settings())
    bot._bot = SimpleNamespace()  # пройти guard `if self._bot is None`
    bot._username = "my_shop_bot"
    q = _fake_inline_query("a")
    await bot._on_inline_query(q)
    q.answer.assert_called_once()
    kwargs = q.answer.call_args.kwargs
    assert kwargs["results"] == []


async def test_inline_finds_in_stock_only(db_setup):
    await _seed(db_setup)
    bot = ShopBot(_settings())
    bot._bot = SimpleNamespace()
    bot._username = "my_shop_bot"
    q = _fake_inline_query("apple")
    await bot._on_inline_query(q)
    q.answer.assert_called_once()
    results = q.answer.call_args.kwargs["results"]
    assert len(results) == 2  # 2 Apple
    # Spotify (OOS) не попал
    for r in results:
        assert "Spotify" not in r.title


async def test_inline_result_article_structure(db_setup):
    await _seed(db_setup)
    bot = ShopBot(_settings())
    bot._bot = SimpleNamespace()
    bot._username = "my_shop_bot"
    q = _fake_inline_query("apple")
    await bot._on_inline_query(q)
    results = q.answer.call_args.kwargs["results"]
    art = results[0]
    # Уникальный id ≤64
    assert len(art.id) <= 64
    # Title в Telegram-лимите 64 байта (примерно — символы ≤64)
    assert len(art.title) <= 64
    # input_message_content — там ссылка на бот
    msg_text = art.input_message_content.message_text
    assert "https://t.me/my_shop_bot" in msg_text


async def test_inline_excludes_oos_explicit_query(db_setup):
    await _seed(db_setup)
    bot = ShopBot(_settings())
    bot._bot = SimpleNamespace()
    bot._username = "my_shop_bot"
    q = _fake_inline_query("spotify")
    await bot._on_inline_query(q)
    results = q.answer.call_args.kwargs["results"]
    # Spotify был, но OOS → не выдаётся
    assert results == []
