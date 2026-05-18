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
    assert _apply_premium(100.0, RateMode.AUTO, 2.0) == pytest.approx(102.0)


def test_apply_premium_zero_premium_keeps_base():
    assert _apply_premium(100.0, RateMode.AUTO, 0.0) == pytest.approx(100.0)


def test_apply_premium_manual_mode_ignores_premium():
    """В manual-режиме оператор сам выставил итоговый курс — премия не нужна."""
    assert _apply_premium(100.0, RateMode.MANUAL, 5.0) == pytest.approx(100.0)


def test_rate_breakdown_dataclass_has_premium_flag():
    rb = RateBreakdown(base=72.0, premium_percent=2.0, effective=73.44, source="cbr")
    assert rb.has_premium is True
    no_prem = RateBreakdown(base=72.0, premium_percent=0.0, effective=72.0, source="manual")
    assert no_prem.has_premium is False


def _patch_premium(monkeypatch, value: float) -> None:
    """Подменить runtime-чтение premium (которое лезет в БД)."""
    async def fake_premium(settings=None):
        return value
    import src.config_runtime as cr
    monkeypatch.setattr(cr, "get_premium_percent", fake_premium)


@pytest.mark.asyncio
async def test_get_rate_breakdown_manual_mode_returns_user_rate(monkeypatch):
    _patch_premium(monkeypatch, 0.0)  # в MANUAL не должно использоваться, но на всякий
    s = _settings(mode=RateMode.MANUAL, premium=49.0, manual=85.0)
    rb = await get_rate_breakdown(s)
    assert rb.source == "manual"
    assert rb.base == pytest.approx(85.0)
    assert rb.effective == pytest.approx(85.0)
    assert rb.premium_percent == 0.0


@pytest.mark.asyncio
async def test_get_rate_breakdown_auto_uses_premium(monkeypatch):
    """При успешном fetch from CBR — premium прибавляется."""
    _patch_premium(monkeypatch, 2.0)
    s = _settings(mode=RateMode.AUTO, premium=2.0)
    monkeypatch.setattr(fx, "_cache", None)

    async def fake_fetch():
        return 72.0

    monkeypatch.setattr(fx, "_fetch_cbr_rate", fake_fetch)

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
    _patch_premium(monkeypatch, 2.0)
    s = _settings(mode=RateMode.AUTO, premium=2.0)
    monkeypatch.setattr(fx, "_cache", (time.time(), 70.0))

    rb = await get_rate_breakdown(s)
    assert rb.source == "cache_mem"
    assert rb.base == pytest.approx(70.0)
    assert rb.effective == pytest.approx(70.0 * 1.02)


@pytest.mark.asyncio
async def test_get_rate_breakdown_runtime_override_used(monkeypatch):
    """
    Главное свойство runtime-override: даже если в .env premium=2%,
    но в БД лежит 4% — берём 4%.
    """
    _patch_premium(monkeypatch, 4.0)  # эмулируем override в БД
    s = _settings(mode=RateMode.AUTO, premium=2.0)
    monkeypatch.setattr(fx, "_cache", (time.time(), 100.0))

    rb = await get_rate_breakdown(s)
    assert rb.premium_percent == pytest.approx(4.0)
    assert rb.effective == pytest.approx(104.0)


async def _async_noop(*args, **kwargs):
    return None
