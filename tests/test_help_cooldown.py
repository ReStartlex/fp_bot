"""
Cooldown на !помощь: после первого help-ack бот молчит 3 минуты (180 сек)
по умолчанию, чтобы покупатель не задолбал и продавца, и сам себя
повторами "!помощь !помощь !помощь".
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.chat.handler import ChatHandler
from src.config import Settings
from src.db.models import Base
from src.funpay.events import FunPayMessageEvent


@pytest_asyncio.fixture()
async def session_factory_fixture(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.chat.handler.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _settings(help_cooldown: int = 180) -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        chat_help_cooldown_seconds=help_cooldown,
    )


def _make_handler(settings: Settings):
    fp = MagicMock()
    fp.my_username = "lol228822"
    fp.account = SimpleNamespace(id=1, username="lol228822")
    fp.send_message = AsyncMock()
    tg = MagicMock()
    tg.send = AsyncMock()
    return ChatHandler(fp, telegram=tg, settings=settings), fp, tg


def _event(text: str) -> FunPayMessageEvent:
    return FunPayMessageEvent(
        chat_id=42, chat_username="VOLT", author_id=2,
        author_username="VOLT", text=text, is_my_message=False,
    )


@pytest.mark.asyncio
async def test_first_help_triggers_ack_and_tg(session_factory_fixture):
    handler, fp, tg = _make_handler(_settings(help_cooldown=180))
    await handler.on_message(_event("!помощь"))
    fp.send_message.assert_called_once()
    tg.send.assert_called_once()


@pytest.mark.asyncio
async def test_second_help_within_cooldown_is_silent(session_factory_fixture):
    handler, fp, tg = _make_handler(_settings(help_cooldown=180))
    await handler.on_message(_event("!помощь"))
    fp.send_message.reset_mock()
    tg.send.reset_mock()
    # второй "!помощь" сразу — должен быть проигнорирован
    await handler.on_message(_event("!помощь"))
    fp.send_message.assert_not_called()
    tg.send.assert_not_called()


@pytest.mark.asyncio
async def test_help_after_cooldown_zero_always_fires(session_factory_fixture):
    """Если cooldown=0, дедуп help-ack отключён (= legacy-поведение)."""
    handler, fp, tg = _make_handler(_settings(help_cooldown=0))
    await handler.on_message(_event("!помощь"))
    await handler.on_message(_event("!помощь"))
    assert fp.send_message.call_count == 2
    assert tg.send.call_count == 2
