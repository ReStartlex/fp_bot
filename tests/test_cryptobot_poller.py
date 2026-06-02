"""
Тесты polling-воркера. CryptoBot HTTP мокается (фейковый client),
БД — реальная in-memory SQLite.

Сценарии:
  1. paid invoice без записи в ShopPayment → skipped (мы её не создавали);
  2. paid invoice с записью pending → applied=1, баланс начислен;
  3. два прогона подряд → второй ничего не начисляет (skipped=1);
  4. сетевая ошибка getInvoices → errors=1, applied=0, никаких side-effect'ов;
  5. notifier вызывается с правильным telegram_id и текстом.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.db.models import Base
from src.db.session import _get_engine
from src.shop.payments.cryptobot import (
    CryptoBotClient,
    CryptoBotError,
    Invoice,
)
from src.shop.payments.poller import (
    _decimal_rub_to_kopecks,
    _parse_iso_or_none,
    poll_cryptobot_once,
)
from src.shop.repo import (
    create_topup_payment,
    get_balance_stats,
    get_or_create_user,
    get_payment,
)


# ─── unit helpers ───────────────────────────────────────────────────


def test_rub_to_kopecks_integer():
    assert _decimal_rub_to_kopecks(Decimal("500")) == 50000


def test_rub_to_kopecks_decimal():
    assert _decimal_rub_to_kopecks(Decimal("500.50")) == 50050


def test_rub_to_kopecks_rounding():
    # Округление до целого копейки (нет «полу-копеек»)
    assert _decimal_rub_to_kopecks(Decimal("0.005")) == 0  # ROUND_HALF_EVEN
    assert _decimal_rub_to_kopecks(Decimal("0.015")) == 2


def test_parse_iso_z():
    dt = _parse_iso_or_none("2026-05-25T18:00:00.000Z")
    assert dt is not None
    assert dt.year == 2026


def test_parse_iso_none():
    assert _parse_iso_or_none(None) is None
    assert _parse_iso_or_none("garbage") is None


# ─── poll integration tests ────────────────────────────────────────


class _FakeClient:
    """Имитация CryptoBotClient без сети."""

    def __init__(self, invoices: list[Invoice], raise_on_get: bool = False):
        self._invoices = invoices
        self._raise = raise_on_get

    async def get_invoices(self, **kwargs):
        if self._raise:
            raise CryptoBotError(500, "server_error")
        return list(self._invoices)


def _make_invoice(
    invoice_id: int, amount_rub: str = "500", status: str = "paid",
) -> Invoice:
    return Invoice.from_api({
        "invoice_id": invoice_id,
        "status": status,
        "amount": amount_rub,
        "fiat": "RUB",
        "bot_invoice_url": f"https://t.me/CryptoBot?start=I_{invoice_id}",
        "paid_at": "2026-05-25T18:00:00.000Z",
    })


@pytest.fixture()
async def db_setup(monkeypatch):
    """
    Подменяем глобальный engine/session_factory на in-memory SQLite,
    чтобы poll_cryptobot_once мог использовать session_factory()().

    Settings — immutable Pydantic; для подмены api_token используем
    отдельный SimpleNamespace со всеми нужными полями. poller их
    читает явно (getattr на settings), поэтому Namespace ок.
    """
    import src.db.session as session_mod
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    # Сохраняем и сбрасываем глобальные синглтоны
    saved_engine = session_mod._engine
    saved_factory = session_mod._session_factory

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_mod._engine = engine
    session_mod._session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession,
    )

    from pydantic import SecretStr
    settings = SimpleNamespace(
        cryptobot_api_token=SecretStr("tt"),
        cryptobot_testnet=False,
    )
    yield settings

    await engine.dispose()
    session_mod._engine = saved_engine
    session_mod._session_factory = saved_factory


async def test_poll_skips_unknown_invoice(db_setup):
    """CryptoBot вернул paid invoice, которого нет в нашей БД → skipped."""
    client = _FakeClient([_make_invoice(999, "100")])
    result = await poll_cryptobot_once(settings=db_setup, client=client)
    assert result.checked == 1
    assert result.applied == 0
    assert result.skipped == 1


async def test_poll_applies_paid_payment(db_setup):
    """Создан pending payment → poller'у CryptoBot вернул paid → начислено."""
    from src.db.session import session_factory
    async with session_factory()() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=100500)
        u_id = u.id
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="123",
            amount_kopecks=50000,
            notify_telegram_id=100500,
        )
        await s.commit()

    client = _FakeClient([_make_invoice(123, "500")])
    result = await poll_cryptobot_once(settings=db_setup, client=client)
    assert result.applied == 1

    async with session_factory()() as s:
        stats = await get_balance_stats(s, user_id=u_id)
        assert stats.current_kopecks == 50000


async def test_poll_is_idempotent(db_setup):
    """Второй прогон с тем же paid invoice не приводит к двойному начислению."""
    from src.db.session import session_factory
    async with session_factory()() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=42)
        u_id = u.id
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="555",
            amount_kopecks=10000,
        )
        await s.commit()

    client = _FakeClient([_make_invoice(555, "100")])
    r1 = await poll_cryptobot_once(settings=db_setup, client=client)
    r2 = await poll_cryptobot_once(settings=db_setup, client=client)
    assert r1.applied == 1
    assert r2.applied == 0
    assert r2.skipped == 1
    async with session_factory()() as s:
        stats = await get_balance_stats(s, user_id=u_id)
        assert stats.current_kopecks == 10000  # NOT 20000


async def test_poll_handles_network_error(db_setup):
    """Сетевая ошибка → errors=1, никаких side-effects."""
    client = _FakeClient([], raise_on_get=True)
    result = await poll_cryptobot_once(settings=db_setup, client=client)
    assert result.checked == 0
    assert result.errors == 1
    assert result.applied == 0


async def test_poll_calls_notifier(db_setup):
    """notifier(tg_id, text) вызывается на applied платёж."""
    from src.db.session import session_factory
    async with session_factory()() as s:
        u, _ = await get_or_create_user(s, telegram_user_id=88)
        await create_topup_payment(
            s, user_id=u.id, provider="cryptobot",
            provider_invoice_id="777",
            amount_kopecks=30000,
            notify_telegram_id=88,
        )
        await s.commit()

    captured: list[tuple[int, str]] = []

    async def notifier(tg_id: int, text: str):
        captured.append((tg_id, text))

    client = _FakeClient([_make_invoice(777, "300")])
    result = await poll_cryptobot_once(
        settings=db_setup, client=client, notifier=notifier,
    )
    assert result.applied == 1
    assert len(captured) == 1
    tg_id, text = captured[0]
    assert tg_id == 88
    assert "300" in text
    assert "777" in text


async def test_poll_when_token_missing(db_setup):
    """Без api_token → возвращаем 0,0,0,0 без обращения к сети."""
    db_setup.cryptobot_api_token = None
    result = await poll_cryptobot_once(settings=db_setup)
    assert result.checked == 0
    assert result.applied == 0
    assert result.skipped == 0
    assert result.errors == 0
