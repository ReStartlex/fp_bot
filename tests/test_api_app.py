from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import app as api_app
from src.config import Settings, get_settings


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        ns_user_id=1,
        ns_login="x",
        ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x",
        funpay_user_id=1,
        web_api_enabled=True,
        web_api_token="secret-token",
    )


def test_healthz_is_public(monkeypatch):
    async def noop():
        return None

    monkeypatch.setattr(api_app, "init_db", noop)
    monkeypatch.setattr(api_app, "close_db", noop)
    app = api_app.create_app()

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_requires_bearer_token(monkeypatch):
    async def noop():
        return None

    async def fake_dashboard(_settings):
        return {"service": "test"}

    monkeypatch.setattr(api_app, "init_db", noop)
    monkeypatch.setattr(api_app, "close_db", noop)
    monkeypatch.setattr(api_app, "get_dashboard_summary", fake_dashboard)
    app = api_app.create_app()
    app.dependency_overrides[get_settings] = _settings

    with TestClient(app) as client:
        unauthorized = client.get("/api/dashboard")
        forbidden = client.get(
            "/api/dashboard", headers={"Authorization": "Bearer wrong"}
        )
        ok = client.get(
            "/api/dashboard", headers={"Authorization": "Bearer secret-token"}
        )

    assert unauthorized.status_code == 401
    assert forbidden.status_code == 403
    assert ok.status_code == 200
    assert ok.json() == {"service": "test"}
