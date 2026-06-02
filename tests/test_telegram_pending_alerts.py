"""
TelegramNotifier: retry + SQLite-очередь pending_alerts при сетевых сбоях.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.alerts.telegram import TelegramNotifier
from src.config import Settings
from src.db.models import Base, PendingTelegramAlert
from src.db.repo import list_due_pending_telegram_alerts
from sqlalchemy import select


def _settings(**overrides) -> Settings:
    base = dict(
        ns_user_id=1,
        ns_login="x",
        ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x",
        funpay_user_id=1,
        telegram_enabled=True,
        telegram_bot_token="12345:fake-token",
        telegram_chat_id=999,
        telegram_use_proxy=False,
        telegram_send_max_retries=2,
        telegram_send_retry_base_seconds=0.01,
        telegram_pending_retry_base_seconds=1.0,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


class _FakeClient:
    def __init__(self, *, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict) -> Any:  # noqa: A002
        self.posts.append({"url": url, "json": json})
        if self.behavior == "connect_timeout":
            raise httpx.ConnectTimeout("simulated timeout")
        return _Resp(status_code=200, text="ok")

    async def aclose(self) -> None:
        pass


class _Resp:
    def __init__(self, *, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr("src.db.session._engine", engine, raising=False)
    monkeypatch.setattr("src.db.session._session_factory", factory, raising=False)
    monkeypatch.setattr("src.db.session.session_factory", lambda: factory)

    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_send_enqueues_after_retries_exhausted(monkeypatch, db_factory):
    client = _FakeClient(behavior="connect_timeout")

    def fake_async_client(**kwargs):
        return client

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)

    async with TelegramNotifier(_settings()) as tg:
        ok = await tg.send("✅ Заказ выполнен LTMP5G89")

    assert ok is False
    async with db_factory() as session:
        rows = list(
            (await session.execute(select(PendingTelegramAlert))).scalars().all()
        )
    assert len(rows) == 1
    assert "LTMP5G89" in rows[0].text
    assert len(client.posts) == 2


@pytest.mark.asyncio
async def test_flush_pending_delivers_and_removes(monkeypatch, db_factory):
    ok_client = _FakeClient(behavior="ok")

    def fake_async_client(**kwargs):
        return ok_client

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)

    async with db_factory() as session:
        session.add(
            PendingTelegramAlert(
                text="queued alert",
                parse_mode="HTML",
                retry_count=0,
                next_retry_at=datetime.utcnow() - timedelta(seconds=5),
            )
        )
        await session.commit()

    async with TelegramNotifier(_settings()) as tg:
        stats = await tg.flush_pending_alerts()

    assert stats["sent"] == 1
    async with db_factory() as session:
        due = await list_due_pending_telegram_alerts(session, limit=10)
    assert due == []
