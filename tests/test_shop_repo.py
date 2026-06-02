"""
Тесты shop/repo: get_or_create_user (idempotent), attach_referral
(защита от self-ref и от двойной привязки), apply_balance_change
(инвариант ledger==balance, защита от ухода в минус), parse_referral_payload.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, ShopBalanceLedger, ShopReferral, ShopUser
from src.shop.repo import (
    apply_balance_change,
    attach_referral,
    get_or_create_user,
    parse_referral_payload,
)


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


# ─── parse_referral_payload ─────────────────────────────────────────


def test_parse_referral_none_for_empty():
    assert parse_referral_payload(None) is None
    assert parse_referral_payload("") is None
    assert parse_referral_payload("   ") is None


def test_parse_referral_simple_digit():
    assert parse_referral_payload("123") == 123


def test_parse_referral_with_prefix():
    assert parse_referral_payload("ref_42") == 42


def test_parse_referral_rejects_non_digit():
    assert parse_referral_payload("abc") is None
    assert parse_referral_payload("ref_abc") is None
    assert parse_referral_payload("12abc") is None


def test_parse_referral_rejects_non_positive():
    assert parse_referral_payload("0") is None
    assert parse_referral_payload("-5") is None


# ─── get_or_create_user ─────────────────────────────────────────────


async def test_get_or_create_creates_new(factory):
    async with factory() as s:
        user, is_new = await get_or_create_user(
            s, telegram_user_id=100, first_name="Alice", language_code="ru"
        )
        await s.commit()
        assert is_new is True
        assert user.id is not None
        assert user.telegram_user_id == 100
        assert user.first_name == "Alice"
        assert user.balance_kopecks == 0


async def test_get_or_create_returns_existing(factory):
    async with factory() as s:
        u1, new1 = await get_or_create_user(s, telegram_user_id=100, first_name="A")
        await s.commit()
    async with factory() as s:
        u2, new2 = await get_or_create_user(s, telegram_user_id=100, first_name="B")
        await s.commit()
        assert new1 is True
        assert new2 is False
        assert u1.id == u2.id
        # Имя обновилось — Telegram-имя пользователя могло измениться
        assert u2.first_name == "B"


# ─── attach_referral ────────────────────────────────────────────────


async def test_attach_referral_creates_record(factory):
    async with factory() as s:
        a, _ = await get_or_create_user(s, telegram_user_id=1, first_name="A")
        b, _ = await get_or_create_user(s, telegram_user_id=2, first_name="B")
        await s.commit()
        ref = await attach_referral(
            s, referrer_user_id=a.id, referred_user_id=b.id
        )
        await s.commit()
        assert ref is not None
        # Дублирование в shop_users
        await s.refresh(b)
        assert b.referred_by_user_id == a.id


async def test_attach_referral_rejects_self(factory):
    async with factory() as s:
        a, _ = await get_or_create_user(s, telegram_user_id=1, first_name="A")
        await s.commit()
        ref = await attach_referral(
            s, referrer_user_id=a.id, referred_user_id=a.id
        )
        assert ref is None


async def test_attach_referral_idempotent(factory):
    """Повторная попытка привязать того же реферала — no-op, без падения."""
    async with factory() as s:
        a, _ = await get_or_create_user(s, telegram_user_id=1, first_name="A")
        b, _ = await get_or_create_user(s, telegram_user_id=2, first_name="B")
        c, _ = await get_or_create_user(s, telegram_user_id=3, first_name="C")
        await s.commit()
        ref1 = await attach_referral(s, referrer_user_id=a.id, referred_user_id=b.id)
        await s.commit()
        # Попытка перепривязать к c — игнор, b остаётся с inviter=a
        ref2 = await attach_referral(s, referrer_user_id=c.id, referred_user_id=b.id)
        await s.commit()
        assert ref1 is not None
        assert ref2 is None
        await s.refresh(b)
        assert b.referred_by_user_id == a.id


# ─── apply_balance_change ───────────────────────────────────────────


async def test_apply_balance_change_credits(factory):
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=1)
        await s.commit()
        await apply_balance_change(
            s, user_id=u.id, change_kopecks=500,
            reason="referral_cashback", note="invite x",
        )
        await s.commit()
        await s.refresh(u)
        assert u.balance_kopecks == 500
        rows = (await s.execute(select(ShopBalanceLedger))).scalars().all()
        assert len(rows) == 1
        assert rows[0].change_kopecks == 500


async def test_apply_balance_change_debit_invariant(factory):
    """sum(ledger) == user.balance после серии credit/debit."""
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=1)
        await s.commit()
        for change, reason in [
            (1000, "manual_admin"),
            (-200, "order_payment"),
            (50, "referral_cashback"),
            (-300, "order_payment"),
        ]:
            await apply_balance_change(
                s, user_id=u.id, change_kopecks=change, reason=reason
            )
        await s.commit()
        await s.refresh(u)
        rows = (
            await s.execute(
                select(ShopBalanceLedger).where(ShopBalanceLedger.user_id == u.id)
            )
        ).scalars().all()
        assert u.balance_kopecks == sum(r.change_kopecks for r in rows)
        assert u.balance_kopecks == 1000 - 200 + 50 - 300


async def test_apply_balance_change_rejects_overdraft(factory):
    """Защита от ухода в минус — критично для предотвращения race-double-spend."""
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=1)
        await s.commit()
        await apply_balance_change(
            s, user_id=u.id, change_kopecks=100, reason="manual_admin"
        )
        await s.commit()
        with pytest.raises(ValueError, match="insufficient"):
            await apply_balance_change(
                s, user_id=u.id, change_kopecks=-150, reason="order_payment"
            )


async def test_apply_balance_change_zero_is_noop(factory):
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=1)
        await s.commit()
        await apply_balance_change(
            s, user_id=u.id, change_kopecks=0, reason="manual_admin"
        )
        await s.commit()
        rows = (await s.execute(select(ShopBalanceLedger))).scalars().all()
        assert rows == []
