"""
Тесты на критический фикс: _apply_decision больше не проглатывает
неудачный save_lot.

История: новые HTTP-метрики (см. test_funpay_http_metrics) показали
   exhausted=1
в проде, но lots_skipped был 0 — значит, save_lot вернул
{"ok": False, "funpay_error": "...429..."}, но _apply_decision этого
не проверял. lots_updated++ инкрементился ложно — мониторинг врал.

Этот тест-сьют гарантирует:
  * dict с ok=False → SaveLotFailed (тип ошибки + наличие диагностики)
  * dict с ok=True  → нет исключения
  * dict без ключа "ok" → нет исключения (бэк-совместимость)
  * None / другие типы → нет исключения (защита от изменения контракта)
  * сообщение исключения содержит lot_id, http_status и funpay_error
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.sync.stock_sync import (
    LotSyncDecision,
    SaveLotFailed,
    _apply_decision,
)
from src.mapping.rules import PricingResult
from src.config import Currency


class _FakeLotFields:
    """Минимальный mock LotFields с теми атрибутами, что трогает _apply_decision."""

    def __init__(self) -> None:
        self.price: float | None = 100.0
        self.amount: int = 5
        self.active: bool = True
        self.fields = {"price": "100", "amount": "5"}


class _FakeFunPayClient:
    """
    Мок FunPayClient для _apply_decision.

    Параметризуется тем, что должен вернуть save_lot.
    Записывает все вызовы, чтобы тесты могли проверить контракт.
    """

    def __init__(self, save_result: Any = None) -> None:
        self._save_result = save_result
        self.get_calls: list[int] = []
        self.save_calls: list[Any] = []

    async def get_lot_fields(self, lot_id: int, node_id: int | None = None) -> _FakeLotFields:
        self.get_calls.append(lot_id)
        return _FakeLotFields()

    async def save_lot(self, lot_fields: _FakeLotFields) -> Any:
        self.save_calls.append(lot_fields)
        return self._save_result


def _make_decision(*, lot_id: int = 12345) -> LotSyncDecision:
    """LotSyncDecision с реальным действием (изменение цены), чтобы
    _apply_decision дошёл до save_lot."""
    target = PricingResult(
        ns_price_usd=1.0,
        fx_rate=73.0,
        markup_percent=5.0,
        price_target=100.0,
        stock=5,
        currency=Currency.RUB,
    )
    return LotSyncDecision(
        funpay_lot_id=lot_id,
        ns_service_id=99,
        label="test lot",
        current_price=80.0,
        target=target,
        will_update_price=True,
        will_update_stock=False,
        will_activate=False,
        will_deactivate=False,
    )


def _make_settings() -> SimpleNamespace:
    """Минимальный settings (любые атрибуты, к которым обращается _apply_decision)."""
    return SimpleNamespace(
        funpay_currency="RUB",
        funpay_update_rate_limit_per_second=10,
    )


# ─────────────── контракт save_lot=ok=False ───────────────


@pytest.mark.asyncio
async def test_apply_decision_raises_when_save_lot_returns_ok_false():
    """Главный кейс: save_lot вернул {"ok": False, ...} → SaveLotFailed."""
    fp = _FakeFunPayClient(save_result={
        "ok": False,
        "http_status": 429,
        "funpay_error": "FunPay rate-limit 429 после 5 попыток",
    })
    decision = _make_decision(lot_id=42)
    settings = _make_settings()

    with pytest.raises(SaveLotFailed) as exc_info:
        await _apply_decision(decision, fp, settings)

    err_msg = str(exc_info.value)
    # Должно содержать lot_id, http статус, и funpay_error для диагностики.
    assert "42" in err_msg, "lot_id должен быть в сообщении ошибки"
    assert "429" in err_msg, "http_status должен быть в сообщении"
    assert "rate-limit" in err_msg.lower(), "funpay_error должен быть в сообщении"


@pytest.mark.asyncio
async def test_apply_decision_uses_json_when_no_funpay_error_field():
    """save_lot{"ok": False, "json": {"msg": "что-то странное"}} —
    тоже должен попасть в SaveLotFailed."""
    fp = _FakeFunPayClient(save_result={
        "ok": False,
        "http_status": 200,
        "json": {"msg": "лот заблокирован модератором"},
    })
    decision = _make_decision(lot_id=999)
    settings = _make_settings()

    with pytest.raises(SaveLotFailed) as exc_info:
        await _apply_decision(decision, fp, settings)

    err_msg = str(exc_info.value)
    assert "999" in err_msg
    assert "заблокирован" in err_msg


@pytest.mark.asyncio
async def test_apply_decision_no_diagnostic_falls_back_to_unknown():
    """save_lot{"ok": False} без funpay_error/json — текст 'unknown FunPay error'."""
    fp = _FakeFunPayClient(save_result={"ok": False})
    decision = _make_decision()
    settings = _make_settings()

    with pytest.raises(SaveLotFailed) as exc_info:
        await _apply_decision(decision, fp, settings)

    assert "unknown FunPay error" in str(exc_info.value)


# ─────────────── контракт save_lot=ok=True / другие типы ───────────────


@pytest.mark.asyncio
async def test_apply_decision_passes_when_save_lot_returns_ok_true():
    """save_lot{"ok": True, ...} → нет исключения, save вызван 1 раз."""
    fp = _FakeFunPayClient(save_result={
        "ok": True,
        "http_status": 200,
        "json": {"msg": "ok"},
    })
    decision = _make_decision()
    settings = _make_settings()

    await _apply_decision(decision, fp, settings)  # не должно бросать

    assert len(fp.save_calls) == 1
    assert len(fp.get_calls) == 1


@pytest.mark.asyncio
async def test_apply_decision_passes_when_save_lot_returns_none():
    """save_lot вернул None — считаем успехом (backwards-compat)."""
    fp = _FakeFunPayClient(save_result=None)
    decision = _make_decision()
    settings = _make_settings()

    await _apply_decision(decision, fp, settings)  # не бросать


@pytest.mark.asyncio
async def test_apply_decision_passes_when_save_lot_returns_dict_without_ok_key():
    """save_lot{"http_status": 200} без ключа "ok" — backwards-compat,
    не считаем фейлом (чтобы старые/тестовые клиенты не падали)."""
    fp = _FakeFunPayClient(save_result={"http_status": 200})
    decision = _make_decision()
    settings = _make_settings()

    await _apply_decision(decision, fp, settings)  # не бросать


# ─────────────── интеграция с try/except в run_sync_once ───────────────


def test_save_lot_failed_is_caught_by_run_sync_once_handler():
    """
    Анти-регрессия: SaveLotFailed должен ловиться `except Exception`
    в run_sync_once (строка ~419), а значит наследоваться от Exception
    (а не от BaseException). Иначе lots_skipped++ не сработает.
    """
    assert issubclass(SaveLotFailed, Exception)
    assert issubclass(SaveLotFailed, RuntimeError)  # тоже OK, унаследован


def test_save_lot_failed_has_docstring_mentioning_metrics():
    """Чтобы будущий читатель понял ПОЧЕМУ существует это исключение
    (контекст важен — раньше fail глотался, метрики помогли увидеть)."""
    assert SaveLotFailed.__doc__ is not None
    doc = SaveLotFailed.__doc__.lower()
    # должны быть хотя бы упоминания: метрики и rate-limit как причина.
    assert "save_lot" in doc
    assert "429" in doc or "rate-limit" in doc or "exhaust" in doc
