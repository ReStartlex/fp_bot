"""
P2-6: rate-limit на auth-ручках сайта (брутфорс-барьер).

После N запросов с одного IP на /auth/* — 429. Лимитер in-memory, свой
на каждое приложение (app.state), per-key path:ip.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.ratelimit import InMemoryRateLimiter


@pytest.fixture()
def app_env(monkeypatch):
    monkeypatch.setenv("FUNPAY_GOLDEN_KEY", "x" * 64)
    monkeypatch.setenv("SHOP_ENABLED", "true")
    monkeypatch.setenv("SHOP_TELEGRAM_BOT_TOKEN", "123:TESTTOKEN")
    monkeypatch.setenv("SITE_AUTH_RATE_LIMIT", "5")
    monkeypatch.setenv("SITE_AUTH_RATE_WINDOW_SECONDS", "60")
    import src.config as cfg
    monkeypatch.setattr(cfg, "_settings", None)
    yield
    monkeypatch.setattr(cfg, "_settings", None)


def test_unit_limiter_sliding_window():
    lim = InMemoryRateLimiter()
    # лимит 3 в большом окне: первые 3 ok, 4-й — отказ
    assert lim.allow("k", 3, 1000) is True
    assert lim.allow("k", 3, 1000) is True
    assert lim.allow("k", 3, 1000) is True
    assert lim.allow("k", 3, 1000) is False
    # другой ключ независим
    assert lim.allow("other", 3, 1000) is True


def test_auth_endpoint_rate_limited(app_env):
    client = TestClient(create_app())
    # 5 разрешено (лимит 5), даже если payload невалидный (401/400),
    # rate-limit срабатывает ДО логики; 6-й → 429.
    statuses = []
    for _ in range(6):
        r = client.post("/api/site/auth/telegram", json={"bad": "payload"})
        statuses.append(r.status_code)
    assert statuses[-1] == 429, statuses
    assert statuses.count(429) == 1, statuses
    # первые 5 — не 429 (отбились валидацией/логином, но не лимитом)
    assert all(s != 429 for s in statuses[:5]), statuses


def test_auth_rate_limit_per_endpoint_independent(app_env):
    client = TestClient(create_app())
    # исчерпываем /auth/telegram
    for _ in range(6):
        client.post("/api/site/auth/telegram", json={"x": 1})
    # /auth/oauth — отдельный ключ (path), ещё не лимитирован
    r = client.post("/api/site/auth/oauth", json={"x": 1})
    assert r.status_code != 429
