"""
Unit-тесты handler'ов shop-бота. aiogram-bot не поднимается, а handler'ы
вызываются напрямую с фейковым `Message` — это паттерн из
tests/test_pending_confirm_cmd.py для owner-бота.

Покрываем:
- /start без рефералки → юзер создаётся, без referral.
- /start ref_X → юзер создаётся, привязка к inviter'у X.
- /start ref_X для уже зарегистрированного → НЕ привязывается заново.
- /start ref_<self_id> → защита от self-referral.
- /balance → возвращает 0 ₽ для нового, корректный баланс для существующего.
- /ref → построение реф-ссылки.
- enabled flag → False если shop_telegram_bot_token не задан.
- format_rub → корректное форматирование.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base, ShopReferral, ShopUser
from src.shop.bot import ShopBot, format_rub
from src.shop.repo import apply_balance_change, get_or_create_user


# ─── format_rub ─────────────────────────────────────────────────────


def test_format_rub_zero():
    assert format_rub(0) == "0\u00a0₽"


def test_format_rub_small():
    assert format_rub(100) == "1\u00a0₽"
    assert format_rub(150) == "1,50\u00a0₽"


def test_format_rub_big():
    assert format_rub(1_234_500) == "12\u00a0345\u00a0₽"
    assert format_rub(123_456_789) == "1\u00a0234\u00a0567,89\u00a0₽"


# ─── enabled flag ───────────────────────────────────────────────────


def _settings(shop_enabled=True, with_token=True) -> Settings:
    base: dict = dict(
        ns_user_id=1, ns_login="x", ns_password="x", ns_api_secret="QQ==",
        funpay_golden_key="x", funpay_user_id=1,
        telegram_bot_token=None,
        shop_enabled=shop_enabled,
        shop_telegram_bot_token="dummy-shop-token" if with_token else None,
    )
    return Settings(**base)  # type: ignore[call-arg]


def test_shop_bot_disabled_when_flag_false():
    bot = ShopBot(_settings(shop_enabled=False))
    assert bot.enabled is False


def test_shop_bot_disabled_when_no_token():
    bot = ShopBot(_settings(shop_enabled=True, with_token=False))
    assert bot.enabled is False


def test_shop_bot_enabled_when_both_set():
    bot = ShopBot(_settings(shop_enabled=True, with_token=True))
    assert bot.enabled is True


# ─── /start, /balance, /ref handlers ────────────────────────────────


@pytest.fixture()
async def db_setup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.shop.bot.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _fake_user(tg_id: int, username: str | None = None, first_name: str = "U"):
    return SimpleNamespace(
        id=tg_id,
        username=username,
        first_name=first_name,
        language_code="ru",
    )


def _fake_message(tg_user) -> SimpleNamespace:
    """Минимальный Message-like объект: from_user + answer()."""
    msg = SimpleNamespace()
    msg.from_user = tg_user
    msg.answer = AsyncMock()
    return msg


def _fake_command(payload: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=payload)


async def test_start_creates_new_user_without_ref(db_setup):
    bot = ShopBot(_settings())
    bot._username = "shop_bot"
    msg = _fake_message(_fake_user(100, username="alice", first_name="Alice"))
    await bot._on_start(msg, _fake_command(None))

    async with db_setup() as s:
        users = (await s.execute(select(ShopUser))).scalars().all()
        refs = (await s.execute(select(ShopReferral))).scalars().all()
        assert len(users) == 1
        assert users[0].telegram_user_id == 100
        assert users[0].first_name == "Alice"
        assert refs == []
    msg.answer.assert_called_once()
    # Приветственное сообщение содержит имя
    text = msg.answer.call_args.args[0]
    assert "Alice" in text


async def test_start_attaches_referral(db_setup):
    bot = ShopBot(_settings())
    bot._username = "shop_bot"

    # 1) inviter регистрируется
    msg_a = _fake_message(_fake_user(1, first_name="A"))
    await bot._on_start(msg_a, _fake_command(None))
    async with db_setup() as s:
        inviter = (
            await s.execute(select(ShopUser).where(ShopUser.telegram_user_id == 1))
        ).scalar_one()
        inviter_id = inviter.id

    # 2) реферал заходит по ссылке /start ref_{inviter_id}
    msg_b = _fake_message(_fake_user(2, first_name="B"))
    await bot._on_start(msg_b, _fake_command(f"ref_{inviter_id}"))

    async with db_setup() as s:
        refs = (await s.execute(select(ShopReferral))).scalars().all()
        assert len(refs) == 1
        assert refs[0].referrer_user_id == inviter_id
        invited = (
            await s.execute(select(ShopUser).where(ShopUser.telegram_user_id == 2))
        ).scalar_one()
        assert invited.referred_by_user_id == inviter_id
    # В приветственном сообщении упомянут кэшбэк рефералу
    text = msg_b.answer.call_args.args[0]
    assert "1%" in text


async def test_start_does_not_reattach_referral_on_revisit(db_setup):
    """Если юзер уже зарегистрирован — реф-ссылка игнорируется (защита от смены inviter'а)."""
    bot = ShopBot(_settings())
    bot._username = "shop_bot"

    # Сначала зарегистрировался без ref
    msg = _fake_message(_fake_user(5, first_name="X"))
    await bot._on_start(msg, _fake_command(None))

    # Потом пришёл по реф-ссылке — игнор
    msg2 = _fake_message(_fake_user(5, first_name="X"))
    await bot._on_start(msg2, _fake_command("ref_999"))

    async with db_setup() as s:
        refs = (await s.execute(select(ShopReferral))).scalars().all()
        assert refs == []


async def test_start_rejects_self_referral(db_setup):
    """Юзер с deep-link на самого себя — реф не привязывается."""
    bot = ShopBot(_settings())
    bot._username = "shop_bot"

    # Регистрируем
    msg = _fake_message(_fake_user(7, first_name="S"))
    await bot._on_start(msg, _fake_command(None))

    async with db_setup() as s:
        u = (
            await s.execute(select(ShopUser).where(ShopUser.telegram_user_id == 7))
        ).scalar_one()

    # Юзер удалил локальный аккаунт (теоретический сценарий) и снова /start с ref_{self.id} — мы фактически
    # уже зарегистрированы, parse_referral отработает, но в _on_start блок ref срабатывает
    # только для is_new=True. Так что повторно не привяжется. Однако более прямой тест —
    # симулируем что система пытается attach_referral с одинаковыми id (изоляция unit-уровня).
    from src.shop.repo import attach_referral
    async with db_setup() as s:
        result = await attach_referral(s, referrer_user_id=u.id, referred_user_id=u.id)
        assert result is None


async def test_balance_handler_zero_for_new_user(db_setup):
    bot = ShopBot(_settings())
    msg = _fake_message(_fake_user(11, first_name="Z"))
    await bot._on_balance(msg)
    text = msg.answer.call_args.args[0]
    assert "0\u00a0₽" in text


async def test_balance_handler_shows_actual_balance(db_setup):
    bot = ShopBot(_settings())
    # Регистрируем юзера и начисляем баланс
    msg_init = _fake_message(_fake_user(33, first_name="R"))
    await bot._on_start(msg_init, _fake_command(None))
    async with db_setup() as s:
        u = (
            await s.execute(select(ShopUser).where(ShopUser.telegram_user_id == 33))
        ).scalar_one()
        await apply_balance_change(
            s, user_id=u.id, change_kopecks=12345,
            reason="referral_cashback",
        )
        await s.commit()

    msg = _fake_message(_fake_user(33, first_name="R"))
    await bot._on_balance(msg)
    text = msg.answer.call_args.args[0]
    # 12345 копеек = 123,45 ₽
    assert "123,45" in text


async def test_ref_handler_returns_link(db_setup):
    bot = ShopBot(_settings())
    bot._username = "my_shop_bot"
    msg = _fake_message(_fake_user(42, first_name="L"))
    await bot._on_ref(msg)
    text = msg.answer.call_args.args[0]
    # Ссылка вида https://t.me/my_shop_bot?start=ref_1
    assert "https://t.me/my_shop_bot?start=ref_" in text


async def test_ref_handler_waits_when_bot_not_started_yet(db_setup):
    bot = ShopBot(_settings())
    bot._username = None  # типа ещё не выполнили get_me
    msg = _fake_message(_fake_user(42, first_name="L"))
    await bot._on_ref(msg)
    text = msg.answer.call_args.args[0]
    assert "Ещё не готов" in text or "минут" in text.lower()
