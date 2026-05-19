"""
Тесты команды /setmarkup. Главное:
- принимает целые ('6'), дробные с точкой ('5.5'), дробные с запятой ('5,5'),
  c хвостовым процентом ('5.5%');
- 'default'/'none' сбрасывают индивидуальный markup в NULL;
- сообщение пользователю содержит подсказку, если новое значение совпадает
  с эффективным default.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.alerts.bot import TelegramBot
from src.config import Settings
from src.db.models import Mapping
from src.db.repo import upsert_mapping
from src.db.session import close_db, init_db, session_factory


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1,
        ns_login="x",
        ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x",
        funpay_user_id=1,
        markup_percent=6.0,
    )


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    import src.db.session as sess_mod

    sess_mod._engine = None
    sess_mod._session_factory = None

    from src.config import Settings, get_settings

    get_settings()
    orig_data = Settings.data_path.fget
    monkeypatch.setattr(
        Settings, "data_path", property(lambda self: tmp_path), raising=True
    )
    yield
    monkeypatch.setattr(Settings, "data_path", property(orig_data), raising=True)
    sess_mod._engine = None
    sess_mod._session_factory = None


async def _seed_mapping(lot_id: int) -> None:
    async with session_factory()() as session:
        await upsert_mapping(
            session,
            funpay_lot_id=lot_id,
            ns_service_id=20,
            markup_percent=None,
            stock_cap=None,
            ns_fields_template='{"quantity":"@QUANTITY"}',
            enabled=True,
            label="Test",
        )
        await session.commit()


async def _run(text: str, lot_id: int = 69300023) -> tuple[float | None, str]:
    bot = TelegramBot(settings=_settings())
    msg = SimpleNamespace(text=text, answer=AsyncMock())
    await bot._do_setmarkup(msg)  # type: ignore[arg-type]
    async with session_factory()() as session:
        obj = (
            await session.execute(select(Mapping).where(Mapping.funpay_lot_id == lot_id))
        ).scalar_one_or_none()
    assert obj is not None
    reply_text = msg.answer.call_args.args[0] if msg.answer.call_args else ""
    return obj.markup_percent, reply_text


def _async(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_setmarkup_accepts_integer():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            value, text = await _run("/setmarkup 69300023 7")
            assert value == pytest.approx(7.0)
            assert "7%" in text
            assert "default 6%" in text
        finally:
            await close_db()
    _async(go())


def test_setmarkup_accepts_fractional_dot():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            value, text = await _run("/setmarkup 69300023 5.5")
            assert value == pytest.approx(5.5)
            assert "5.5%" in text
        finally:
            await close_db()
    _async(go())


def test_setmarkup_accepts_fractional_comma():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            value, text = await _run("/setmarkup 69300023 5,5")
            assert value == pytest.approx(5.5)
            assert "5.5%" in text
        finally:
            await close_db()
    _async(go())


def test_setmarkup_accepts_percent_suffix():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            value, _ = await _run("/setmarkup 69300023 7.25%")
            assert value == pytest.approx(7.25)
        finally:
            await close_db()
    _async(go())


def test_setmarkup_default_resets_to_null():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            # сначала ставим число, потом сбрасываем
            await _run("/setmarkup 69300023 12")
            value, text = await _run("/setmarkup 69300023 default")
            assert value is None
            assert "default" in text.lower()
        finally:
            await close_db()
    _async(go())


def test_setmarkup_equals_default_shows_hint():
    """6% при default=6% — sync скажет updated=0; пользователь должен это понимать."""
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            _, text = await _run("/setmarkup 69300023 6")
            assert "равна default" in text or "равна <b>default</b>" in text
        finally:
            await close_db()
    _async(go())


def test_setmarkup_different_value_no_hint():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            _, text = await _run("/setmarkup 69300023 5.5")
            assert "равна default" not in text
        finally:
            await close_db()
    _async(go())
