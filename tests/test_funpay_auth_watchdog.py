"""
P0-3: watchdog протухания golden_key.

Покрываем ядро механизма:
  1. sync_once при FunPayAuthError из get_lot_fields НЕ падает, а
     помечает лот auth_error и возвращает result["auth_errors"] > 0
     (это сигнал, по которому _safe_sync шлёт алерт «обнови golden_key»).
  2. FunPayClient.check_auth возвращает трёхзначный статус:
     "authed" — распарсили user_id; "logged_out" — есть маркер логина
     (форма/редирект); "unknown" — ни то ни другое (транзиент/сеть/
     смена вёрстки). unknown НЕ трактуется как разлогин — это и был
     источник false positive (инцидент 2026-06-13).
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.db.models import Base
from src.db.repo import upsert_mapping
from src.funpay.admin_http import FunPayAuthError
from src.funpay.client import FunPayClient
from src.ns.models import Category, Service, StockResponse
import src.sync.stock_sync as ss


def _settings(**overrides) -> Settings:
    base = dict(
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
        enable_real_actions=False,
        telegram_bot_token=None, telegram_use_proxy=False,
        funpay_currency="RUB",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


@pytest.fixture()
async def db_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.sync.stock_sync.session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def patch_fx(monkeypatch):
    async def _fx(_settings=None):
        return 75.0
    monkeypatch.setattr(ss, "get_usd_rub_rate", _fx)


class _AuthFailFP:
    """FunPay-клиент с протухшим golden_key: get_lot_fields кидает auth."""
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None
    async def connect(self): return None

    async def get_lot_fields(self, lot_id: int, node_id=None):
        raise FunPayAuthError("FunPay перебросил на форму логина — обнови golden_key")

    async def save_lot(self, lot_fields):
        return {"ok": True}

    def get_and_reset_http_metrics(self):
        return {"ok": 0, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}


class _NS:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None

    async def get_stock(self):
        return StockResponse(categories=[
            Category(category_id=1, category_name="Apple", services=[
                Service(service_id=42, service_name="Apple 10 TRY",
                        price=0.5, currency="usd", in_stock=100),
            ]),
        ])


async def test_sync_once_surfaces_auth_errors(db_factory, monkeypatch):
    settings = _settings()
    monkeypatch.setattr("src.sync.stock_sync.get_settings", lambda: settings)

    async with db_factory() as s:
        await upsert_mapping(
            s, funpay_lot_id=1, ns_service_id=42,
            markup_percent=10.0, stock_cap=100,
            ns_fields_template='{"q":"@QUANTITY"}',
            enabled=True, label="Apple 10 TRY",
        )
        await s.commit()

    result = await ss.sync_once(funpay_client=_AuthFailFP(), ns_client=_NS())

    # sync не упал, лот пропущен, auth_errors просигналил
    assert result["auth_errors"] == 1
    assert result["skipped"] == 1
    assert result["updated"] == 0


# ───────────── check_auth ─────────────

class _FakeAdmin:
    def __init__(
        self,
        authed: bool | None,
        raise_exc: bool = False,
        login_marker: bool = False,
    ):
        self._authed = authed
        self._raise = raise_exc
        self._login_marker = login_marker

    async def whoami(self):
        if self._raise:
            raise RuntimeError("network down")
        return {
            "authenticated": self._authed,
            "user_id": 617001 if self._authed else None,
            "login_marker": self._login_marker,
            "http_status": 200,
            "final_url": "https://funpay.com/",
        }


def _client_with_admin(admin) -> FunPayClient:
    fp = FunPayClient(_settings())
    fp._admin_client_cache = admin  # property отдаёт кеш, реальный HTTP не трогаем
    return fp


@pytest.mark.asyncio
async def test_check_auth_authed_when_user_id_parsed():
    fp = _client_with_admin(_FakeAdmin(authed=True))
    status, _reason = await fp.check_auth()
    assert status == "authed"


@pytest.mark.asyncio
async def test_check_auth_logged_out_only_with_login_marker():
    """Явный маркер логина (форма/редирект) → точно разлогинены."""
    fp = _client_with_admin(_FakeAdmin(authed=False, login_marker=True))
    status, _reason = await fp.check_auth()
    assert status == "logged_out"


@pytest.mark.asyncio
async def test_check_auth_unknown_when_no_user_id_no_marker():
    """user_id не распознан, но и маркера логина нет → unknown (не разлогин).

    Это ядро фикса инцидента 2026-06-13: один whoami-fail при живой
    сессии не должен трактоваться как протухший golden_key.
    """
    fp = _client_with_admin(_FakeAdmin(authed=False, login_marker=False))
    status, _reason = await fp.check_auth()
    assert status == "unknown"


@pytest.mark.asyncio
async def test_check_auth_unknown_on_network_error():
    """Сетевой сбой whoami != «ключ протух» — unknown, не паникуем."""
    fp = _client_with_admin(_FakeAdmin(authed=None, raise_exc=True))
    status, _reason = await fp.check_auth()
    assert status == "unknown"


# ───────────── streak-механизм алерта (инцидент 2026-06-13) ─────────────

class _RecordingTg:
    """Записывает доставленные алерты, чтобы проверить, что и когда летит."""
    def __init__(self):
        self.errors: list[str] = []
        self.infos: list[str] = []

    async def error(self, text: str):
        self.errors.append(text)

    async def info(self, text: str):
        self.infos.append(text)


def _app_for_streak(confirm_failures: int = 2):
    from src.main import App
    app = App.__new__(App)  # минуем тяжёлый __init__ (get_settings/клиенты)
    app.settings = _settings(
        funpay_auth_watchdog_confirm_failures=confirm_failures,
        funpay_auth_watchdog_alert_cooldown_seconds=3600,
    )
    app.tg = _RecordingTg()
    app._funpay_auth_fail_streak = 0
    app._last_golden_key_alert = None
    return app


@pytest.mark.asyncio
async def test_single_auth_failure_does_not_alert():
    """Один whoami-fail (streak 1 < порог 2) НЕ шлёт алерт — это и есть
    фикс false positive: одиночный сбой при живой сессии молчит."""
    app = _app_for_streak(confirm_failures=2)
    await app._register_funpay_auth_failure(context="whoami: logged_out")
    assert app._funpay_auth_fail_streak == 1
    assert app.tg.errors == []
    assert app._last_golden_key_alert is None


@pytest.mark.asyncio
async def test_two_confirmations_trigger_alert():
    """Подтверждение из 2 источников/циклов → алерт «golden_key протух»."""
    app = _app_for_streak(confirm_failures=2)
    await app._register_funpay_auth_failure(context="whoami: logged_out")
    await app._register_funpay_auth_failure(context="2 auth-ошибок в sync")
    assert app._funpay_auth_fail_streak == 2
    assert len(app.tg.errors) == 1
    assert "golden_key" in app.tg.errors[0]


@pytest.mark.asyncio
async def test_auth_ok_resets_streak_and_sends_recovery():
    """Удачный авторизованный sync сбрасывает streak; если алертили —
    летит «✅ восстановлено»."""
    app = _app_for_streak(confirm_failures=2)
    await app._register_funpay_auth_failure(context="x")
    await app._register_funpay_auth_failure(context="y")
    assert len(app.tg.errors) == 1  # был алерт

    await app._register_funpay_auth_ok()
    assert app._funpay_auth_fail_streak == 0
    assert app._last_golden_key_alert is None
    assert len(app.tg.infos) == 1
    assert "восстановлена" in app.tg.infos[0]


@pytest.mark.asyncio
async def test_auth_ok_without_prior_alert_is_silent():
    """Сброс streak без предшествующего алерта не шлёт «✅» (не флудим)."""
    app = _app_for_streak(confirm_failures=2)
    await app._register_funpay_auth_failure(context="single blip")
    await app._register_funpay_auth_ok()  # streak был 1, алерта не было
    assert app._funpay_auth_fail_streak == 0
    assert app.tg.infos == []
