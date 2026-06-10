"""
Тесты сборки aiogram-сессии с прокси (src/alerts/aiogram_proxy.py).

Контекст: на РФ-VPS direct до api.telegram.org блокируется, long-polling
ботов обязан идти через SOCKS5-прокси. Эти тесты фиксируют:
  1. Нет прокси-настроек → None (direct), без падений.
  2. Прокси задан, но aiohttp_socks отсутствует → None + не падаем
     (graceful degradation, понятный лог).
  3. Прокси задан и пакет есть → AiohttpSession (если установлен).
"""
from __future__ import annotations

import sys

import pytest

from src.alerts.aiogram_proxy import build_aiogram_session
from src.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        ns_user_id=1, ns_login="x", ns_password="x", ns_api_secret="QQ==",
        funpay_golden_key="x", funpay_user_id=1,
        telegram_bot_token="123:abc",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


def test_no_proxy_returns_none():
    """Без TELEGRAM_USE_PROXY — direct (None), без ошибок."""
    s = _settings(telegram_use_proxy=False)
    assert build_aiogram_session(s, bot_label="admin") is None


def test_proxy_without_host_returns_none():
    """use_proxy=true, но host/port не заданы → telegram_proxy_url=None."""
    s = _settings(telegram_use_proxy=True)
    assert build_aiogram_session(s, bot_label="admin") is None


def test_socks_proxy_without_aiohttp_socks_degrades_gracefully(monkeypatch):
    """
    SOCKS5 задан, но aiohttp_socks не установлен → None (а не краш).
    Симулируем отсутствие пакета через подмену import.
    """
    monkeypatch.setitem(sys.modules, "aiohttp_socks", None)
    s = _settings(
        telegram_use_proxy=True,
        telegram_proxy_type="socks5",
        telegram_proxy_host="1.2.3.4",
        telegram_proxy_port=1080,
    )
    assert build_aiogram_session(s, bot_label="shop") is None


def test_socks_proxy_with_aiohttp_socks_builds_session():
    """
    SOCKS5 задан и aiohttp_socks РЕАЛЬНО установлен → возвращается
    AiohttpSession. Фейковый модуль не годится: aiogram при создании
    сессии сам требует настоящий aiohttp_socks (ProxyConnector), поэтому
    тест пропускается, если пакета нет в окружении (он есть на VPS через
    requirements.txt). Локально без пакета срабатывает graceful
    degradation — это покрыто отдельным тестом выше.
    """
    pytest.importorskip("aiohttp_socks")
    s = _settings(
        telegram_use_proxy=True,
        telegram_proxy_type="socks5",
        telegram_proxy_host="1.2.3.4",
        telegram_proxy_port=1080,
    )
    session = build_aiogram_session(s, bot_label="admin")
    assert session is not None
    assert session.__class__.__name__ == "AiohttpSession"
