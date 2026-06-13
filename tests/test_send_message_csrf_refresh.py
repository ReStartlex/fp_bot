"""
Регрессия (инцидент JK6JW57J, 2026-06-13): send_chat_message ретраил с
тем же закэшированным CSRF-токеном, когда FunPay отвечал «Обновите
страницу и повторите попытку» (= токен протух). Все попытки падали на
мёртвом токене → доставка заказа не уходила, пришлось выдавать руками.

Фикс: при таком ответе сбрасываем кэш csrf, и следующая попытка
перевыпускает токен (через whoami). Здесь это проверяем.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.funpay.admin_http import FunPayAdminClient, _looks_like_stale_csrf


def _admin() -> FunPayAdminClient:
    return FunPayAdminClient(golden_key="x", phpsessid=None)


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)
        self.headers: dict[str, str] = {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        return json.loads(self.text)


def test_looks_like_stale_csrf_matches_funpay_refresh_msg():
    assert _looks_like_stale_csrf(
        '{"msg":"Обновите страницу и повторите попытку.","error":1}'
    )
    assert _looks_like_stale_csrf("Обновите страницу")
    assert _looks_like_stale_csrf(None, "повторите попытку")


def test_looks_like_stale_csrf_negatives():
    assert not _looks_like_stale_csrf()
    assert not _looks_like_stale_csrf(None, None)
    assert not _looks_like_stale_csrf('{"response":{}}', "ok")


@pytest.mark.asyncio
async def test_whoami_takes_csrf_from_app_data_not_meta():
    """csrf для /runner/ берётся из body[data-app-data] (как FunPayAPI), а
    НЕ из meta[name=csrf-token] — это разные токены, и /runner/ валидирует
    именно app-data (инцидент NTZ3MLCY: meta-токен → «Обновите страницу»)."""
    admin = _admin()
    html = (
        "<html><body "
        "data-app-data='{\"userId\": 617001, \"csrf-token\": \"RUNNER_TOK\"}' "
        "data-user-id=\"617001\">"
        "<div class=\"user-link-name\">lol228822</div>"
        "<meta name=\"csrf-token\" content=\"WRONG_META_TOK\">"
        "</body></html>"
    )

    class _Resp:
        text = html
        status_code = 200
        url = "https://funpay.com/"
        headers: dict = {}

    admin._sync_get = lambda url: _Resp()  # type: ignore[assignment]

    me = await admin.whoami()

    assert me["csrf_token"] == "RUNNER_TOK"
    assert admin._csrf_token == "RUNNER_TOK"
    assert me["user_id"] == 617001
    assert me["authenticated"] is True


@pytest.mark.asyncio
async def test_whoami_falls_back_to_meta_when_no_app_data():
    """Если data-app-data отсутствует — берём meta-токен (деградация)."""
    admin = _admin()
    html = (
        "<html><body data-user-id=\"617001\">"
        "<div class=\"user-link-name\">lol228822</div>"
        "<meta name=\"csrf-token\" content=\"META_TOK\">"
        "</body></html>"
    )

    class _Resp:
        text = html
        status_code = 200
        url = "https://funpay.com/"
        headers: dict = {}

    admin._sync_get = lambda url: _Resp()  # type: ignore[assignment]

    me = await admin.whoami()
    assert me["csrf_token"] == "META_TOK"
    assert me["user_id"] == 617001


@pytest.mark.asyncio
async def test_send_chat_message_refreshes_stale_csrf(monkeypatch):
    """Первый ответ = «Обновите страницу» (stale csrf) → токен сбрасывается
    и перевыпускается; вторая попытка идёт с новым токеном и проходит."""
    admin = _admin()
    admin._csrf_token = "stale"

    sent_tokens: list[str | None] = []
    responses = [
        _FakeResp(400, {"msg": "Обновите страницу и повторите попытку.", "error": 1}),
        _FakeResp(200, {"response": {}}),
    ]

    def _post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        sent_tokens.append(data.get("csrf_token"))
        return responses[len(sent_tokens) - 1]

    admin._session = MagicMock()
    admin._session.post = _post

    async def _fake_whoami():
        admin._csrf_token = "fresh"
        return {"csrf_token": "fresh", "authenticated": True}

    monkeypatch.setattr(admin, "whoami", _fake_whoami)
    # не ждём backoff в тесте
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr("src.funpay.admin_http.asyncio.sleep", _no_sleep)

    result = await admin.send_chat_message(123, "доставка")

    assert result["ok"] is True
    # первая попытка — со старым токеном, вторая — с перевыпущенным
    assert sent_tokens == ["stale", "fresh"]


@pytest.mark.asyncio
async def test_send_chat_message_invalidates_csrf_even_when_exhausted(monkeypatch):
    """Даже если все попытки вернули «Обновите страницу», кэш csrf в итоге
    сброшен — следующий ВЫЗОВ метода стартует с перевыпуска (не залипаем
    на мёртвом токене до перезапуска процесса)."""
    admin = _admin()
    admin._csrf_token = "stale"

    def _post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        return _FakeResp(
            400, {"msg": "Обновите страницу и повторите попытку.", "error": 1}
        )

    admin._session = MagicMock()
    admin._session.post = _post

    async def _fake_whoami():
        # whoami тоже не отдаёт свежий (сервер ещё не отдаёт новый) —
        # имитируем худший случай, но проверяем что кэш всё равно сброшен
        return {"csrf_token": None, "authenticated": True}

    monkeypatch.setattr(admin, "whoami", _fake_whoami)
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr("src.funpay.admin_http.asyncio.sleep", _no_sleep)

    result = await admin.send_chat_message(123, "доставка", retries=2)

    assert result["ok"] is False
    assert admin._csrf_token is None  # кэш сброшен, не залип
