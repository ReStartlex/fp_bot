"""
Тесты статистики баланса и рефералов.
- get_balance_stats: текущий / заработано / потрачено / операций
- list_balance_history: пагинация, сортировка new-first
- get_referral_stats: приглашённые / заработано / активные за 30д
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, ShopUser
from src.shop.repo import (
    apply_balance_change,
    attach_referral,
    get_balance_stats,
    get_or_create_user,
    get_referral_stats,
    list_balance_history,
)


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


# ─── balance stats ──────────────────────────────────────────────────


async def test_balance_stats_for_new_user(factory):
    async with factory() as s:
        user, _ = await get_or_create_user(s, telegram_user_id=1)
        await s.commit()
        stats = await get_balance_stats(s, user_id=user.id)
    assert stats.current_kopecks == 0
    assert stats.total_earned_kopecks == 0
    assert stats.total_spent_kopecks == 0
    assert stats.operations_count == 0


async def test_balance_stats_after_credits_and_debits(factory):
    async with factory() as s:
        user, _ = await get_or_create_user(s, telegram_user_id=1)
        await apply_balance_change(
            s, user_id=user.id, change_kopecks=5000,
            reason="referral_cashback",
        )
        await apply_balance_change(
            s, user_id=user.id, change_kopecks=3000,
            reason="referral_cashback",
        )
        await apply_balance_change(
            s, user_id=user.id, change_kopecks=-2000,
            reason="order_payment",
        )
        await s.commit()
        stats = await get_balance_stats(s, user_id=user.id)
    assert stats.current_kopecks == 6000
    assert stats.total_earned_kopecks == 8000  # 5000 + 3000
    assert stats.total_spent_kopecks == 2000   # |-2000|
    assert stats.operations_count == 3


async def test_balance_stats_unknown_user_returns_zeros(factory):
    async with factory() as s:
        stats = await get_balance_stats(s, user_id=99999)
    assert stats == type(stats)(0, 0, 0, 0)


# ─── balance history ────────────────────────────────────────────────


async def test_balance_history_pagination(factory):
    async with factory() as s:
        user, _ = await get_or_create_user(s, telegram_user_id=1)
        for i in range(15):
            await apply_balance_change(
                s, user_id=user.id, change_kopecks=100 + i,
                reason="test",
            )
        await s.commit()
        rows, total = await list_balance_history(s, user_id=user.id, limit=10, offset=0)
        assert len(rows) == 10
        assert total == 15
        # Должны быть отсортированы по убыванию created_at — позже добавлены первыми
        amounts = [r.change_kopecks for r in rows]
        assert amounts == sorted(amounts, reverse=True)


async def test_balance_history_empty_for_no_ops(factory):
    async with factory() as s:
        user, _ = await get_or_create_user(s, telegram_user_id=1)
        await s.commit()
        rows, total = await list_balance_history(s, user_id=user.id)
    assert rows == []
    assert total == 0


# ─── referral stats ─────────────────────────────────────────────────


async def test_referral_stats_for_user_with_no_invites(factory):
    async with factory() as s:
        user, _ = await get_or_create_user(s, telegram_user_id=1)
        await s.commit()
        stats = await get_referral_stats(s, user_id=user.id)
    assert stats.invited_count == 0
    assert stats.total_earned_kopecks == 0
    assert stats.active_referrals_count == 0


async def test_referral_stats_counts_invites_and_earnings(factory):
    async with factory() as s:
        inviter, _ = await get_or_create_user(s, telegram_user_id=100)
        # Приглашаем троих
        for tg_id in (200, 201, 202):
            invited, _ = await get_or_create_user(s, telegram_user_id=tg_id)
            await attach_referral(
                s, referrer_user_id=inviter.id, referred_user_id=invited.id,
            )
        # И inviter получил кэшбэк 2 раза
        await apply_balance_change(
            s, user_id=inviter.id, change_kopecks=500,
            reason="referral_cashback", related_order_id=1,
        )
        await apply_balance_change(
            s, user_id=inviter.id, change_kopecks=700,
            reason="referral_cashback", related_order_id=2,
        )
        # Параллельно — самостоятельное пополнение (НЕ должно засчитаться)
        await apply_balance_change(
            s, user_id=inviter.id, change_kopecks=10000,
            reason="manual_topup",
        )
        await s.commit()
        stats = await get_referral_stats(s, user_id=inviter.id)
    assert stats.invited_count == 3
    assert stats.total_earned_kopecks == 1200  # 500 + 700; manual_topup исключён
    assert stats.active_referrals_count == 3  # все только что зарегистрированы


async def test_referral_stats_excludes_inactive_referrals(factory):
    async with factory() as s:
        inviter, _ = await get_or_create_user(s, telegram_user_id=100)
        invited, _ = await get_or_create_user(s, telegram_user_id=200)
        await attach_referral(
            s, referrer_user_id=inviter.id, referred_user_id=invited.id,
        )
        # Делаем referral неактивным (last_seen полгода назад)
        invited.last_seen_at = datetime.utcnow() - timedelta(days=180)
        await s.commit()
        stats = await get_referral_stats(s, user_id=inviter.id)
    assert stats.invited_count == 1
    assert stats.active_referrals_count == 0  # за 30д не было активности
