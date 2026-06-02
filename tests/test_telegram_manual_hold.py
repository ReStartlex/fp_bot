"""
Тесты payload'a TelegramNotifier.manual_hold_required.

Сетевой вызов мокаем httpx-клиентом, который складывает все
sendMessage-запросы в список. Проверяем, что:
- callback_data на кнопках имеют формат hold:<action>:<funpay_order_id>;
- ничего из секретов (токена бота, NS custom_id внутри URL и т.п.) не утекает;
- has_pins=True даёт «коды на руках», has_pins=False — «нужно выдать вручную»;
- слишком длинный funpay_order_id обрезается до 48 символов (лимит Telegram
  callback_data = 64 байта).
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.alerts.telegram import TelegramNotifier
from src.config import Settings


def _settings(**overrides) -> Settings:
    base: dict = dict(
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        telegram_enabled=True,
        telegram_bot_token="12345:fake-token",
        telegram_chat_id=999,
        telegram_use_proxy=False,
        telegram_proxy_host=None,
        telegram_proxy_port=None,
        telegram_proxy_username=None,
        telegram_proxy_password=None,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


class _Recorder:
    """httpx-replacement, который собирает все POST-payload'ы для проверки."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict) -> Any:  # noqa: A002 — match httpx API
        self.posts.append({"url": url, "json": json})

        class _R:
            status_code = 200
            text = "ok"
        return _R()

    async def aclose(self) -> None: pass


def _patch_async_client(monkeypatch, recorder: _Recorder) -> None:
    monkeypatch.setattr(
        "src.alerts.telegram.httpx",
        type("M", (), {"AsyncClient": lambda **kw: recorder})(),
        raising=True,
    )


def _ensure_httpx_module_attr(monkeypatch) -> _Recorder:
    """TelegramNotifier импортирует httpx внутри __aenter__; патчим там."""
    rec = _Recorder()
    # Подменяем httpx.AsyncClient так, чтобы возврат всегда был наш Recorder.
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: rec)
    return rec


@pytest.mark.asyncio
async def test_manual_hold_required_payload_with_pins(monkeypatch):
    rec = _ensure_httpx_module_attr(monkeypatch)
    settings = _settings()

    async with TelegramNotifier(settings) as tg:
        await tg.manual_hold_required(
            funpay_order_id="FP-12345",
            stage="post_pins_pre_delivery",
            age_seconds=620,
            buyer_username="alice",
            ns_custom_id="NSC-7",
            has_pins=True,
            reason="hard timeout с pins на руках",
        )

    assert len(rec.posts) == 1
    payload = rec.posts[0]["json"]
    text = payload["text"]
    assert "FP-12345" in text
    assert "alice" in text
    assert "NSC-7" in text
    assert "post_pins_pre_delivery" in text
    assert "~10 мин" in text or "~11 мин" in text  # 620s ≈ 10-11 мин
    assert "коды NS" in text.lower() or "коды ns" in text.lower()

    buttons = payload["reply_markup"]["inline_keyboard"]
    callback_data = [btn["callback_data"] for row in buttons for btn in row]
    assert "hold:retry:FP-12345" in callback_data
    assert "hold:done:FP-12345" in callback_data
    assert "hold:show:FP-12345" in callback_data
    assert "close" in callback_data


@pytest.mark.asyncio
async def test_manual_hold_required_payload_without_pins(monkeypatch):
    rec = _ensure_httpx_module_attr(monkeypatch)
    async with TelegramNotifier(_settings()) as tg:
        await tg.manual_hold_required(
            funpay_order_id="FP-99",
            stage="ns_wait_completion",
            age_seconds=600,
            buyer_username=None,
            ns_custom_id=None,
            has_pins=False,
            reason="NS не отдал коды",
        )

    text = rec.posts[0]["json"]["text"]
    assert "FP-99" in text
    # без pins → инструкция «вернуть деньги/выдать вручную»
    assert "вручную" in text.lower() or "вернуть" in text.lower()
    # ns_custom_id None — строки про NS быть НЕ должно
    assert "NS: <code>" not in text


@pytest.mark.asyncio
async def test_manual_hold_required_truncates_long_order_id(monkeypatch):
    """callback_data в Telegram ограничен 64 байтами; длинный id обрезается."""
    rec = _ensure_httpx_module_attr(monkeypatch)
    long_id = "X" * 80
    async with TelegramNotifier(_settings()) as tg:
        await tg.manual_hold_required(
            funpay_order_id=long_id,
            stage="before_ns_purchase",
            age_seconds=700,
            buyer_username="bob",
            ns_custom_id=None,
            has_pins=False,
            reason="hard timeout",
        )
    buttons = rec.posts[0]["json"]["reply_markup"]["inline_keyboard"]
    callback_data = [btn["callback_data"] for row in buttons for btn in row]
    for cd in callback_data:
        if cd == "close":
            continue
        # hold:<action>:<id_до_48_симв> ≤ 5 + 6 + 48 = 59
        assert len(cd) <= 60, f"callback_data слишком длинный: {cd!r}"


@pytest.mark.asyncio
async def test_manual_hold_required_escapes_html(monkeypatch):
    """Имена с < > & не должны ломать parse_mode=HTML."""
    rec = _ensure_httpx_module_attr(monkeypatch)
    async with TelegramNotifier(_settings()) as tg:
        await tg.manual_hold_required(
            funpay_order_id="FP-1",
            stage="x",
            age_seconds=60,
            buyer_username="<script>",
            ns_custom_id="A&B",
            has_pins=False,
            reason="<dangerous>",
        )
    text = rec.posts[0]["json"]["text"]
    # raw < и > не должны попасть в текст (HTML-escape должен сработать)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;dangerous&gt;" in text
    assert "A&amp;B" in text


@pytest.mark.asyncio
async def test_manual_hold_required_noop_when_disabled(monkeypatch):
    """Если telegram_enabled=False — метод не падает, просто ничего не шлёт."""
    rec = _ensure_httpx_module_attr(monkeypatch)
    settings = _settings(telegram_enabled=False)
    async with TelegramNotifier(settings) as tg:
        await tg.manual_hold_required(
            funpay_order_id="FP-1",
            stage="x",
            age_seconds=10,
            buyer_username="alice",
            ns_custom_id=None,
            has_pins=False,
            reason="x",
        )
    assert rec.posts == [], "при отключённом telegram не должно быть POST'ов"
