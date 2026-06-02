"""
Тесты идемпотентного применения CryptoBot-платежей.

Сценарии:
  1. create_topup_payment создаёт ShopPayment в pending;
  2. повторный create с тем же invoice_id возвращает существующий (idempotent);
  3. apply_paid_invoice начисляет баланс ровно один раз;
  4. повторный apply_paid_invoice — no-op (was_just_applied=False);
  5. apply на неизвестный invoice → (None, False);
  6. mark_payment_failed для pending → status=failed;
  7. mark_payment_failed для paid → no-op.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base
from src.shop.repo import (
    apply_paid_invoice,
    create_topup_payment,
    get_balance_stats,
    get_or_create_user,
    get_payment,
    list_pending_payments_for_user,
    mark_payment_failed,
)


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    yield f
    await engine.dispose()


# ─── create_topup_payment ───────────────────────────────────────────


async def test_create_topup_payment_basic(factory):
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=111)
        p = await create_topup_payment(
            s,
            user_id=u.id,
            provider="cryptobot",
            provider_invoice_id="INV-1",
            amount_kopecks=50000,
        )
        await s.commit()
        assert p.status == "pending"
        assert p.amount_kopecks == 50000
        assert p.order_id is None
        assert '"topup_user_id": ' in (p.raw_payload_json or "")


async def test_create_topup_payment_is_idempotent(factory):
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=111)
        p1 = await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="INV-1", amount_kopecks=50000,
        )
        await s.commit()
        p2 = await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="INV-1", amount_kopecks=50000,
        )
        await s.commit()
        assert p1.id == p2.id


# ─── apply_paid_invoice ─────────────────────────────────────────────


async def test_apply_paid_invoice_credits_balance(factory):
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=111)
        u_id = u.id
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="INV-7", amount_kopecks=50000,
        )
        await s.commit()
    async with factory() as s:
        _, applied = await apply_paid_invoice(
            s, provider="cryptobot", provider_invoice_id="INV-7",
            paid_amount_kopecks=50000,
        )
        await s.commit()
        assert applied is True
        stats = await get_balance_stats(s, user_id=u_id)
        assert stats.current_kopecks == 50000
        assert stats.total_earned_kopecks == 50000


async def test_apply_paid_invoice_is_idempotent(factory):
    """Повторный paid-event не приводит к двойному начислению."""
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=222)
        u_id = u.id
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="INV-X", amount_kopecks=30000,
        )
        await s.commit()
    async with factory() as s:
        _, first = await apply_paid_invoice(
            s, provider="cryptobot", provider_invoice_id="INV-X",
            paid_amount_kopecks=30000,
        )
        await s.commit()
    async with factory() as s:
        payment, second = await apply_paid_invoice(
            s, provider="cryptobot", provider_invoice_id="INV-X",
            paid_amount_kopecks=30000,
        )
        await s.commit()
        assert first is True
        assert second is False
        assert payment.status == "paid"
        stats = await get_balance_stats(s, user_id=u_id)
        assert stats.current_kopecks == 30000  # NOT 60000


async def test_apply_paid_invoice_for_unknown_payment(factory):
    async with factory() as s:
        payment, applied = await apply_paid_invoice(
            s, provider="cryptobot", provider_invoice_id="UNKNOWN",
            paid_amount_kopecks=1000,
        )
        await s.commit()
        assert payment is None
        assert applied is False


async def test_apply_paid_invoice_writes_paid_payload(factory):
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=333)
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="INV-PL", amount_kopecks=10000,
        )
        await s.commit()
    async with factory() as s:
        await apply_paid_invoice(
            s, provider="cryptobot", provider_invoice_id="INV-PL",
            paid_amount_kopecks=10000,
            raw_payload_json='{"crypto_paid_amount": "0.5", "asset": "USDT"}',
        )
        await s.commit()
        p = await get_payment(s, provider="cryptobot", provider_invoice_id="INV-PL")
        assert p is not None
        assert "paid_payload" in (p.raw_payload_json or "")
        assert "USDT" in (p.raw_payload_json or "")


# ─── mark_payment_failed ────────────────────────────────────────────


async def test_mark_payment_failed(factory):
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=444)
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="INV-E", amount_kopecks=10000,
        )
        await s.commit()
    async with factory() as s:
        p = await mark_payment_failed(
            s, provider="cryptobot", provider_invoice_id="INV-E",
            reason="expired",
        )
        await s.commit()
        assert p.status == "failed"
        assert p.error == "expired"


async def test_mark_payment_failed_skips_paid(factory):
    """Уже оплаченный платёж нельзя 'failed-нуть'."""
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=555)
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="INV-P", amount_kopecks=10000,
        )
        await s.commit()
    async with factory() as s:
        await apply_paid_invoice(
            s, provider="cryptobot", provider_invoice_id="INV-P",
            paid_amount_kopecks=10000,
        )
        await s.commit()
    async with factory() as s:
        p = await mark_payment_failed(
            s, provider="cryptobot", provider_invoice_id="INV-P",
            reason="too_late",
        )
        await s.commit()
        assert p.status == "paid"  # not changed
        assert p.error is None


# ─── list_pending_payments_for_user ─────────────────────────────────


async def test_list_pending_for_user(factory):
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=777)
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="A", amount_kopecks=1000,
        )
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="B", amount_kopecks=2000,
        )
        await s.commit()
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=777)
        items = await list_pending_payments_for_user(s, user_id=u.id)
        assert len(items) == 2
    async with factory() as s:
        await apply_paid_invoice(
            s, provider="cryptobot", provider_invoice_id="A",
            paid_amount_kopecks=1000,
        )
        await s.commit()
    async with factory() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=777)
        items = await list_pending_payments_for_user(s, user_id=u.id)
        assert len(items) == 1
        assert items[0].provider_invoice_id == "B"
