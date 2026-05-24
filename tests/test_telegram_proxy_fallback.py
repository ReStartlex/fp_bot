"""
TelegramNotifier должен автоматически переключаться на direct, если
SOCKS5/HTTP-прокси из .env становится недоступен с этого VPS.

Боевой кейс (наблюдался на Timeweb VPS 2026-05-25):
- aiogram-бот для приёма команд /menu идёт напрямую (без proxy) и работает.
- TelegramNotifier (httpx) тянет TELEGRAM_PROXY_* из .env и пытается слать
  через `socks5://166.88.218.111:62947`. С этого VPS TCP до прокси не
  открывается → `ConnectTimeout` → все уведомления о продажах, старте,
  остановке тихо теряются.
- Пользователь видит работающее меню, но «не приходят уведомления».

Защита: при network-error через прокси notifier ОДИН раз пробует direct,
и если direct работает — постоянно переключается на него (на этот
процесс). HTTP-ошибки (4xx/5xx от Telegram) НЕ триггерят fallback —
прокси не виноват в плохом payload'е.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.alerts.telegram import TelegramNotifier
from src.config import Settings


def _settings_with_proxy(**overrides) -> Settings:
    base: dict = dict(
        ns_user_id=1,
        ns_login="x",
        ns_password="x",
        ns_api_secret="QQ==",
        funpay_golden_key="x",
        funpay_user_id=1,
        telegram_enabled=True,
        telegram_bot_token="12345:fake-token",
        telegram_chat_id=999,
        telegram_use_proxy=True,
        telegram_proxy_host="166.88.218.111",
        telegram_proxy_port=62947,
        telegram_proxy_username="iRt8qjaa",
        telegram_proxy_password="Wdk3Gycf",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


class _FakeClient:
    """httpx-клиент, поведение которого мы задаём вручную."""

    def __init__(self, *, behavior: str = "ok") -> None:
        # behavior: "ok" | "connect_timeout" | "http_400"
        self.behavior = behavior
        self.posts: list[dict[str, Any]] = []
        self.closed = False

    async def post(self, url: str, json: dict) -> Any:  # noqa: A002
        self.posts.append({"url": url, "json": json})
        if self.behavior == "connect_timeout":
            raise httpx.ConnectTimeout("simulated dead proxy")
        if self.behavior == "http_400":
            return _Resp(status_code=400, text="Bad Request: chat not found")
        return _Resp(status_code=200, text="ok")

    async def aclose(self) -> None:
        self.closed = True


class _Resp:
    def __init__(self, *, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _install_clients(
    monkeypatch,
    *,
    proxy_client: _FakeClient,
    direct_client: _FakeClient,
) -> dict[str, int]:
    """
    Монtkey-patch'им httpx.AsyncClient так, чтобы:
    - вызов с kwarg `proxy=...` возвращал proxy_client;
    - вызов без proxy возвращал direct_client.

    Возвращаем счётчик создания клиентов (для проверки, что direct
    создаётся ЛЕНИВО — только при первом провале proxy).
    """
    counters = {"proxy_created": 0, "direct_created": 0}

    def fake_async_client(**kwargs):
        if "proxy" in kwargs and kwargs["proxy"]:
            counters["proxy_created"] += 1
            return proxy_client
        counters["direct_created"] += 1
        return direct_client

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)
    return counters


# ─────────────── Тесты ───────────────


@pytest.mark.asyncio
async def test_notifier_falls_back_to_direct_when_proxy_connect_timeouts(
    monkeypatch,
):
    """
    Главный кейс: прокси даёт ConnectTimeout → notifier пробует direct
    → direct отвечает 200 → send() возвращает True.
    """
    proxy = _FakeClient(behavior="connect_timeout")
    direct = _FakeClient(behavior="ok")
    counters = _install_clients(
        monkeypatch, proxy_client=proxy, direct_client=direct
    )

    async with TelegramNotifier(_settings_with_proxy()) as tg:
        ok = await tg.send("hello")

    assert ok is True, (
        "Notifier должен сообщить, что доставка успешна, благодаря fallback."
    )
    assert len(proxy.posts) == 1, "Сначала пробовали через прокси"
    assert len(direct.posts) == 1, "Затем direct fallback"
    assert direct.posts[0]["json"]["text"] == "hello"
    assert counters["proxy_created"] == 1
    assert counters["direct_created"] == 1


@pytest.mark.asyncio
async def test_after_proxy_fail_subsequent_sends_skip_proxy(monkeypatch):
    """
    После первого fallback notifier помечает прокси как «мёртвый» и
    следующие сообщения идут СРАЗУ через direct, без бесполезного
    timeout-ожидания на прокси.
    """
    proxy = _FakeClient(behavior="connect_timeout")
    direct = _FakeClient(behavior="ok")
    _install_clients(
        monkeypatch, proxy_client=proxy, direct_client=direct
    )

    async with TelegramNotifier(_settings_with_proxy()) as tg:
        await tg.send("first")
        await tg.send("second")
        await tg.send("third")

    assert len(proxy.posts) == 1, (
        "После первого ConnectTimeout notifier не должен снова дёргать прокси."
    )
    assert len(direct.posts) == 3, "Все три сообщения дошли через direct"
    assert [p["json"]["text"] for p in direct.posts] == [
        "first", "second", "third",
    ]


@pytest.mark.asyncio
async def test_notifier_does_not_fallback_on_http_400(monkeypatch):
    """
    Если Telegram вернул 400 (плохой chat_id и т.п.) — это НЕ
    сетевая проблема, fallback не нужен (он не починит).
    """
    proxy = _FakeClient(behavior="http_400")
    direct = _FakeClient(behavior="ok")
    _install_clients(
        monkeypatch, proxy_client=proxy, direct_client=direct
    )

    async with TelegramNotifier(_settings_with_proxy()) as tg:
        ok = await tg.send("bad chat")

    assert ok is False, "400 от Telegram — не успех"
    assert len(proxy.posts) == 1
    assert len(direct.posts) == 0, (
        "При 400 не пробуем direct: payload-проблема не лечится сменой канала."
    )


@pytest.mark.asyncio
async def test_notifier_without_proxy_does_not_create_extra_clients(
    monkeypatch,
):
    """
    Без TELEGRAM_PROXY_HOST в .env поведение должно остаться прежним:
    один клиент, без direct-fallback-логики.
    """
    proxy = _FakeClient(behavior="ok")  # сюда мы не должны попасть
    direct = _FakeClient(behavior="ok")
    counters = _install_clients(
        monkeypatch, proxy_client=proxy, direct_client=direct
    )

    settings_no_proxy = _settings_with_proxy(
        telegram_use_proxy=False,
        telegram_proxy_host=None,
        telegram_proxy_port=None,
        telegram_proxy_username=None,
        telegram_proxy_password=None,
    )

    async with TelegramNotifier(settings_no_proxy) as tg:
        ok = await tg.send("direct only")

    assert ok is True
    assert counters["proxy_created"] == 0
    assert counters["direct_created"] == 1
    assert len(direct.posts) == 1


@pytest.mark.asyncio
async def test_aclose_closes_both_clients_when_fallback_used(monkeypatch):
    """
    Если fallback задействован, при __aexit__ нужно закрыть оба клиента
    (proxy и direct), чтобы не оставить открытых сокетов.
    """
    proxy = _FakeClient(behavior="connect_timeout")
    direct = _FakeClient(behavior="ok")
    _install_clients(
        monkeypatch, proxy_client=proxy, direct_client=direct
    )

    async with TelegramNotifier(_settings_with_proxy()) as tg:
        await tg.send("trigger fallback")

    assert proxy.closed is True
    assert direct.closed is True
