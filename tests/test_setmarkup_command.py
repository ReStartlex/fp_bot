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


async def _run(
    text: str,
    lot_id: int = 69300023,
    *,
    sync_trigger=None,
) -> tuple[float | None, str]:
    bot = TelegramBot(settings=_settings(), sync_trigger=sync_trigger)
    progress = SimpleNamespace(edit_text=AsyncMock())
    msg = SimpleNamespace(
        text=text,
        answer=AsyncMock(return_value=progress),
        chat=SimpleNamespace(id=123),
        from_user=SimpleNamespace(id=123),
    )
    await bot._do_setmarkup(msg)  # type: ignore[arg-type]
    async with session_factory()() as session:
        obj = (
            await session.execute(select(Mapping).where(Mapping.funpay_lot_id == lot_id))
        ).scalar_one_or_none()
    assert obj is not None
    if progress.edit_text.call_args:
        reply_text = progress.edit_text.call_args.args[0]
    else:
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


def test_setmarkup_runs_sync_immediately_when_available():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            sync = AsyncMock(return_value={"checked": 1, "updated": 1, "skipped": 0})
            value, text = await _run(
                "/setmarkup 69300023 5.5",
                sync_trigger=sync,
            )
            assert value == pytest.approx(5.5)
            sync.assert_awaited_once()
            assert "Цена/остаток применены" in text
            assert "updated=1" in text
        finally:
            await close_db()
    _async(go())


def test_setmarkup_sync_updated_zero_explains_already_current():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            sync = AsyncMock(return_value={"checked": 1, "updated": 0, "skipped": 0})
            _, text = await _run(
                "/setmarkup 69300023 5.5",
                sync_trigger=sync,
            )
            assert "уже совпадает" in text
            assert "updated=0" in text
        finally:
            await close_db()
    _async(go())


def test_plain_text_setmarkup_alias_is_accepted():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            bot = TelegramBot(settings=_settings())
            progress = SimpleNamespace(edit_text=AsyncMock())
            msg = SimpleNamespace(
                text="setmarkup 69300023 5.5",
                answer=AsyncMock(return_value=progress),
                chat=SimpleNamespace(id=123),
                from_user=SimpleNamespace(id=123),
            )
            handled = await bot._dispatch_plain_text_command(msg)  # type: ignore[arg-type]
            async with session_factory()() as session:
                obj = (
                    await session.execute(
                        select(Mapping).where(Mapping.funpay_lot_id == 69300023)
                    )
                ).scalar_one()
            assert handled is True
            assert obj.markup_percent == pytest.approx(5.5)
            msg.answer.assert_awaited()
        finally:
            await close_db()
    _async(go())


def test_reset_markups_can_set_global_default_and_clear_overrides():
    async def go():
        await init_db()
        try:
            await _seed_mapping(69300023)
            await _run("/setmarkup 69300023 9")
            bot = TelegramBot(settings=_settings())
            msg = SimpleNamespace(
                text="/reset_markups 5",
                answer=AsyncMock(),
                chat=SimpleNamespace(id=123),
                from_user=SimpleNamespace(id=123),
            )

            await bot._do_reset_markups(msg)  # type: ignore[arg-type]

            async with session_factory()() as session:
                obj = (
                    await session.execute(
                        select(Mapping).where(Mapping.funpay_lot_id == 69300023)
                    )
                ).scalar_one()
            from src.config_runtime import get_global_markup_percent

            assert obj.markup_percent is None
            assert await get_global_markup_percent(_settings()) == pytest.approx(5.0)
            reply = msg.answer.call_args.args[0]
            assert "5.00%" in reply
        finally:
            await close_db()
    _async(go())


def test_plain_text_unknown_is_not_handled():
    bot = TelegramBot(settings=_settings())
    msg = SimpleNamespace(
        text="просто сообщение",
        answer=AsyncMock(),
        chat=SimpleNamespace(id=123),
        from_user=SimpleNamespace(id=123),
    )
    assert _async(bot._dispatch_plain_text_command(msg)) is False  # type: ignore[arg-type]
