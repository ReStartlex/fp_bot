"""Тесты на расчёт курса USD/RUB с премией к биржевому."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.config import RateMode, Settings
from src.sync import fx
from src.sync.fx import RateBreakdown, _apply_premium, get_rate_breakdown


def _settings(*, mode: RateMode, premium: float, manual: float = 90.0) -> Settings:
    """Сборка реалистичных настроек для тестов курса."""
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==",  # валидный base64 заглушка
        funpay_golden_key="x", funpay_user_id=1,
        usd_rub_rate_mode=mode,
        usd_rub_rate=manual,
        usd_rub_premium_percent=premium,
    )


def test_apply_premium_auto_mode_adds_percent():
    s = _settings(mode=RateMode.AUTO, premium=2.0)
    assert _apply_premium(100.0, s) == pytest.approx(102.0)


def test_apply_premium_zero_premium_keeps_base():
    s = _settings(mode=RateMode.AUTO, premium=0.0)
    assert _apply_premium(100.0, s) == pytest.approx(100.0)


def test_apply_premium_manual_mode_ignores_premium():
    """В manual-режиме оператор сам выставил итоговый курс — премия не нужна."""
    s = _settings(mode=RateMode.MANUAL, premium=5.0)
    assert _apply_premium(100.0, s) == pytest.approx(100.0)


def test_rate_breakdown_dataclass_has_premium_flag():
    rb = RateBreakdown(base=72.0, premium_percent=2.0, effective=73.44, source="cbr")
    assert rb.has_premium is True
    no_prem = RateBreakdown(base=72.0, premium_percent=0.0, effective=72.0, source="manual")
    assert no_prem.has_premium is False


@pytest.mark.asyncio
async def test_get_rate_breakdown_manual_mode_returns_user_rate():
    s = _settings(mode=RateMode.MANUAL, premium=49.0, manual=85.0)
    rb = await get_rate_breakdown(s)
    assert rb.source == "manual"
    assert rb.base == pytest.approx(85.0)
    assert rb.effective == pytest.approx(85.0)
    assert rb.premium_percent == 0.0


@pytest.mark.asyncio
async def test_get_rate_breakdown_auto_uses_premium(monkeypatch):
    """При успешном fetch from CBR — premium прибавляется."""
    s = _settings(mode=RateMode.AUTO, premium=2.0)
    # сбрасываем module-level cache
    monkeypatch.setattr(fx, "_cache", None)

    async def fake_fetch():
        return 72.0

    monkeypatch.setattr(fx, "_fetch_cbr_rate", fake_fetch)

    # Подменяем session_factory, чтобы не дёргать БД
    class _NullCtx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def commit(self): pass

    monkeypatch.setattr(fx, "session_factory", lambda: lambda: _NullCtx())
    monkeypatch.setattr(fx, "save_fx_rate", lambda *a, **kw: _async_noop())

    rb = await get_rate_breakdown(s)
    assert rb.source == "cbr"
    assert rb.base == pytest.approx(72.0)
    assert rb.effective == pytest.approx(72.0 * 1.02)
    assert rb.premium_percent == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_get_rate_breakdown_uses_cache_on_second_call(monkeypatch):
    s = _settings(mode=RateMode.AUTO, premium=2.0)
    monkeypatch.setattr(fx, "_cache", (time.time(), 70.0))

    rb = await get_rate_breakdown(s)
    assert rb.source == "cache_mem"
    assert rb.base == pytest.approx(70.0)
    assert rb.effective == pytest.approx(70.0 * 1.02)


async def _async_noop(*args, **kwargs):
    return None
