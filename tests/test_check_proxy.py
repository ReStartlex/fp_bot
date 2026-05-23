"""Тесты для src/tools/check_proxy.py.

Сетевую часть (_probe / run_checks) подменяем httpx.MockTransport,
чтобы не уходить в реальный github/telegram во время прогонa
тестов. Все «детерминированные» helper'ы (mask_proxy_url,
discover_profiles, summarize, render_matrix) проверяются как
чистые функции.
"""
from __future__ import annotations

from typing import Sequence

import httpx
import pytest

from src.config import ProxyType, Settings
from src.tools import check_proxy as cp
from src.tools.check_proxy import (
    CheckResult,
    Endpoint,
    PROFILE_DIRECT,
    PROFILE_GIT_HTTP,
    PROFILE_SYSTEM,
    PROFILE_TELEGRAM,
    ProxyProfile,
    SMART_MATRIX,
    _should_check,
    build_endpoints,
    discover_profiles,
    mask_proxy_url,
    render_matrix,
    summarize,
)


# ─────────────────────────── фикстуры ───────────────────────────


def _settings(**overrides) -> Settings:
    """Минимальный валидный Settings; параметры можно перегружать поштучно.

    Все telegram_proxy_* выставлены в None явно — иначе тест может
    подцепить значения из локального .env разработчика и стать flaky.
    """
    base: dict = dict(
        ns_user_id=1,
        ns_login="user",
        ns_password="pass",
        ns_api_secret="QQ==",
        funpay_golden_key="g" * 32,
        funpay_user_id=1,
        telegram_bot_token=None,
        telegram_use_proxy=False,
        telegram_proxy_host=None,
        telegram_proxy_port=None,
        telegram_proxy_username=None,
        telegram_proxy_password=None,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


# ─────────────────────────── mask_proxy_url ───────────────────────────


def test_mask_proxy_url_hides_credentials():
    masked = mask_proxy_url("http://modeler_lLeftL:0quI4pXS96Wv@172.235.32.100:10854")
    assert masked is not None
    assert "0quI4pXS96Wv" not in masked
    assert "modeler_lLeftL" not in masked
    assert "172.235.32.100:10854" in masked
    assert masked.startswith("http://mode")
    assert ":***@" in masked


def test_mask_proxy_url_keeps_url_without_creds():
    assert mask_proxy_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert mask_proxy_url(None) is None


# ─────────────────────────── discover_profiles ───────────────────────────


def test_discover_profiles_direct_only_when_nothing_configured():
    profiles = discover_profiles(_settings(), env={})
    assert [p.name for p in profiles] == [PROFILE_DIRECT]


def test_discover_profiles_picks_up_telegram_even_when_use_proxy_false():
    settings = _settings(
        telegram_use_proxy=False,
        telegram_proxy_type=ProxyType.HTTP,
        telegram_proxy_host="172.235.32.100",
        telegram_proxy_port=10854,
        telegram_proxy_username="user",
        telegram_proxy_password="pass",
    )
    profiles = discover_profiles(settings, env={})
    names = [p.name for p in profiles]
    assert PROFILE_TELEGRAM in names
    tg = next(p for p in profiles if p.name == PROFILE_TELEGRAM)
    assert tg.url is not None
    assert "172.235.32.100:10854" in tg.url
    assert "выключен" in tg.source


def test_discover_profiles_skips_telegram_when_host_or_port_missing():
    settings = _settings(
        telegram_use_proxy=True,
        telegram_proxy_host=None,
        telegram_proxy_port=None,
    )
    profiles = discover_profiles(settings, env={})
    assert all(p.name != PROFILE_TELEGRAM for p in profiles)


def test_discover_profiles_uses_explicit_git_proxy_over_env():
    profiles = discover_profiles(
        _settings(),
        explicit_git_proxy="http://cli:cli@cli.example:1",
        env={"GIT_HTTP_PROXY": "http://env:env@env.example:2"},
    )
    git = next(p for p in profiles if p.name == PROFILE_GIT_HTTP)
    assert git.url == "http://cli:cli@cli.example:1"
    assert "CLI" in git.source


def test_discover_profiles_picks_up_system_proxy():
    profiles = discover_profiles(
        _settings(),
        env={"HTTPS_PROXY": "http://sys:sys@sys.example:3"},
    )
    sysp = next(p for p in profiles if p.name == PROFILE_SYSTEM)
    assert sysp.url == "http://sys:sys@sys.example:3"


def test_discover_profiles_can_disable_direct():
    profiles = discover_profiles(_settings(), env={}, include_direct=False)
    assert all(p.name != PROFILE_DIRECT for p in profiles)


# ─────────────────────────── build_endpoints ───────────────────────────


def test_build_endpoints_adds_getme_when_token_present():
    settings = _settings(telegram_bot_token="123:fake-token")
    names = {e.name for e in build_endpoints(settings)}
    assert "telegram-getme" in names
    assert "ns" in names and "funpay" in names and "github" in names


def test_build_endpoints_skips_getme_without_token():
    names = {e.name for e in build_endpoints(_settings())}
    assert "telegram-getme" not in names


def test_build_endpoints_respects_ns_base_url():
    settings = _settings(ns_base_url="https://api-stage.ns.gifts")
    ns_ep = next(e for e in build_endpoints(settings) if e.name == "ns")
    assert ns_ep.url == "https://api-stage.ns.gifts/"


# ─────────────────────────── smart-matrix ───────────────────────────


def test_smart_matrix_blocks_ns_through_git_http():
    assert _should_check(PROFILE_GIT_HTTP, "ns", smart=True) is False


def test_smart_matrix_allows_all_when_disabled():
    assert _should_check(PROFILE_GIT_HTTP, "ns", smart=False) is True


def test_smart_matrix_keeps_external_ip_for_all_profiles():
    for profile in (PROFILE_DIRECT, PROFILE_TELEGRAM, PROFILE_GIT_HTTP, PROFILE_SYSTEM):
        assert "external-ip" in SMART_MATRIX[profile]


# ─────────────────────────── summarize ───────────────────────────


def _ok(profile: str, endpoint: str, *, extra: str | None = None) -> CheckResult:
    return CheckResult(
        profile=profile, endpoint=endpoint, url="x", ok=True,
        status=200, elapsed_s=0.1, extra=extra,
    )


def _fail(profile: str, endpoint: str) -> CheckResult:
    return CheckResult(
        profile=profile, endpoint=endpoint, url="x", ok=False,
        elapsed_s=0.5, error="ConnectError",
    )


def test_summarize_recommends_direct_when_all_green():
    results = [
        _ok(PROFILE_DIRECT, ep) for ep in ("ns", "funpay", "telegram-api", "github", "gh-proxy")
    ]
    notes = summarize(results)
    assert any("прокси можно не включать" in n.lower() for n in notes)


def test_summarize_recommends_telegram_proxy_when_direct_telegram_fails():
    results = [
        _ok(PROFILE_DIRECT, "ns"),
        _ok(PROFILE_DIRECT, "funpay"),
        _fail(PROFILE_DIRECT, "telegram-api"),
        _ok(PROFILE_TELEGRAM, "telegram-api"),
    ]
    notes = summarize(results)
    assert any("TELEGRAM_USE_PROXY=true" in n for n in notes)


def test_summarize_recommends_git_http_when_direct_github_fails():
    results = [
        _ok(PROFILE_DIRECT, "ns"),
        _fail(PROFILE_DIRECT, "github"),
        _ok(PROFILE_GIT_HTTP, "github"),
    ]
    notes = summarize(results)
    assert any("GIT_HTTP_PROXY" in n for n in notes)


def test_summarize_warns_when_proxy_did_not_apply():
    """Если external-ip совпал с direct — прокси на самом деле не сработал."""
    results = [
        _ok(PROFILE_DIRECT, "external-ip", extra="1.2.3.4"),
        _ok(PROFILE_TELEGRAM, "external-ip", extra="1.2.3.4"),
        _ok(PROFILE_DIRECT, "telegram-api"),
        _ok(PROFILE_TELEGRAM, "telegram-api"),
    ]
    notes = summarize(results)
    assert any("НЕ применился" in n for n in notes)


def test_summarize_returns_at_least_one_note():
    notes = summarize([])
    assert notes and isinstance(notes[0], str)


# ─────────────────────────── render_matrix ───────────────────────────


def test_render_matrix_has_endpoint_and_profile_headers():
    results = [
        _ok(PROFILE_DIRECT, "ns"),
        _fail(PROFILE_TELEGRAM, "telegram-api"),
        _ok(PROFILE_DIRECT, "external-ip", extra="9.9.9.9"),
    ]
    out = render_matrix(results)
    assert "endpoint" in out
    assert "direct" in out
    assert "telegram" in out
    assert "ns" in out
    assert "9.9.9.9" in out  # external IP попадает в ячейку
    assert "FAIL" in out


# ─────────────────────────── _probe (сетевая часть с MockTransport) ───────────────────────────


def _direct_profile() -> ProxyProfile:
    return ProxyProfile(name=PROFILE_DIRECT, url=None, source="без прокси")


def _ipify_endpoint() -> Endpoint:
    return Endpoint(
        name="external-ip",
        url="https://api.ipify.org?format=text",
        success_codes=frozenset({200}),
        parse_external_ip=True,
    )


@pytest.mark.asyncio
async def test_probe_returns_external_ip_from_body(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text="203.0.113.7")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(cp.httpx, "AsyncClient", fake_async_client)

    result = await cp._probe(_direct_profile(), _ipify_endpoint(), timeout=2.0)

    assert result.ok is True
    assert result.status == 200
    assert result.extra == "203.0.113.7"
    assert "ipify" in captured["url"]


@pytest.mark.asyncio
async def test_probe_marks_failure_on_http_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(cp.httpx, "AsyncClient", fake_async_client)

    result = await cp._probe(_direct_profile(), _ipify_endpoint(), timeout=2.0)
    assert result.ok is False
    assert result.status is None
    assert result.error and "ConnectError" in result.error


@pytest.mark.asyncio
async def test_probe_treats_any_status_as_ok_for_open_endpoint(monkeypatch):
    """FunPay/NS могут отдать 403 без auth — это всё равно «достучались»."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(cp.httpx, "AsyncClient", fake_async_client)

    ep = Endpoint(name="funpay", url="https://funpay.com/", success_codes=None)
    result = await cp._probe(_direct_profile(), ep, timeout=2.0)
    assert result.ok is True
    assert result.status == 403


@pytest.mark.asyncio
async def test_run_checks_skips_pairs_in_smart_mode(monkeypatch):
    """run_checks не должна дёргать ns через git_http в smart-режиме."""
    seen: list[tuple[str, str]] = []

    async def fake_probe(profile, endpoint, *, timeout):
        seen.append((profile.name, endpoint.name))
        return CheckResult(
            profile=profile.name, endpoint=endpoint.name, url=endpoint.url,
            ok=True, status=200, elapsed_s=0.01,
        )

    monkeypatch.setattr(cp, "_probe", fake_probe)
    profiles = [
        ProxyProfile(name=PROFILE_DIRECT, url=None, source="x"),
        ProxyProfile(name=PROFILE_GIT_HTTP, url="http://x", source="x"),
    ]
    endpoints = [
        Endpoint(name="ns", url="https://api.ns.gifts/"),
        Endpoint(name="github", url="https://github.com/"),
    ]
    results = await cp.run_checks(profiles, endpoints, smart=True)
    assert (PROFILE_GIT_HTTP, "ns") not in seen
    assert (PROFILE_GIT_HTTP, "github") in seen
    assert (PROFILE_DIRECT, "ns") in seen
    assert len(results) == len(seen)


@pytest.mark.asyncio
async def test_run_checks_full_matrix_runs_everything(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def fake_probe(profile, endpoint, *, timeout):
        seen.append((profile.name, endpoint.name))
        return CheckResult(
            profile=profile.name, endpoint=endpoint.name, url=endpoint.url,
            ok=True, status=200, elapsed_s=0.01,
        )

    monkeypatch.setattr(cp, "_probe", fake_probe)
    profiles = [
        ProxyProfile(name=PROFILE_DIRECT, url=None, source="x"),
        ProxyProfile(name=PROFILE_GIT_HTTP, url="http://x", source="x"),
    ]
    endpoints = [Endpoint(name="ns", url="https://api.ns.gifts/")]
    await cp.run_checks(profiles, endpoints, smart=False)
    assert (PROFILE_GIT_HTTP, "ns") in seen
    assert (PROFILE_DIRECT, "ns") in seen
