"""
Тесты на обработку HTTP 429 от FunPay в `FunPayAdminClient`.

Сценарии:
- `_compute_429_backoff`: exponential без Retry-After, числовой Retry-After
  (учитывается), Retry-After больше cap (обрезается), невалидный Retry-After,
  отрицательный Retry-After (fallback на exp).
- `_sync_get`: 429 → 429 → 200 (успех на третьей попытке, делали `time.sleep`
  с правильными интервалами и уважали Retry-After).
- `_sync_get`: 429 везде → бросает HTTPError после исчерпания ретраев.
- `_sync_get`: 200 сразу → нет sleep'ов, один запрос.
- `_sync_post_form_with_429_retry`: 429 → 200 (последняя Response отдана,
  attempts > 1).
- `save_lot`: 429 → 200(json msg=ok) → ok=True; на полном 429-таймауте
  → ok=False, funpay_error содержит "429".

Все мокается через `monkeypatch` на `_session.get/post` — реальный HTTP
никуда не уходит.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from src.funpay.admin_http import FunPayAdminClient, LotFields


# ─────────────── helpers ───────────────


def _make_client(
    *,
    max_429_retries: int = 3,
    base: float = 0.01,  # 10ms — чтобы тесты бежали мгновенно
    max_cap: float = 0.5,
    max_5xx_retries: int = 2,
) -> FunPayAdminClient:
    # rate_min_interval=0 ВАЖНО: иначе rate-limiter добавляет лишние
    # sleep'ы поверх backoff'а, и тесты-счётчики (sleeper.sleeps)
    # ломаются. Здесь мы проверяем именно retry/backoff-логику.
    return FunPayAdminClient(
        golden_key="dummy",
        phpsessid=None,
        max_429_retries=max_429_retries,
        base_429_backoff_seconds=base,
        max_429_backoff_seconds=max_cap,
        max_5xx_retries=max_5xx_retries,
        rate_max_concurrent=8,
        rate_min_interval_seconds=0.0,
    )


class _FakeResponse:
    """Минимальная имитация requests.Response для нужд этих тестов."""

    def __init__(
        self,
        status_code: int = 200,
        body: str = "",
        headers: dict[str, str] | None = None,
        json_data: Any = None,
    ) -> None:
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}
        self._json = json_data
        self.ok = 200 <= status_code < 400

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class _SleepRecorder:
    """Заменяет time.sleep, записывает все sleep'ы и НЕ блокирует тесты."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))


# ─────────────── _compute_429_backoff ───────────────


def test_compute_backoff_exponential_without_retry_after():
    """attempt=0,1,2,3 без Retry-After → 1, 2, 4, 8 (база 1, max 30)."""
    fn = FunPayAdminClient._compute_429_backoff
    assert fn(0, None, 1.0, 30.0) == 1.0
    assert fn(1, None, 1.0, 30.0) == 2.0
    assert fn(2, None, 1.0, 30.0) == 4.0
    assert fn(3, None, 1.0, 30.0) == 8.0


def test_compute_backoff_exponential_capped():
    """Большой attempt → значение не больше max_seconds."""
    fn = FunPayAdminClient._compute_429_backoff
    assert fn(10, None, 1.0, 30.0) == 30.0
    assert fn(20, None, 0.5, 5.0) == 5.0


def test_compute_backoff_retry_after_number_wins_over_exp():
    """Если FunPay прислал Retry-After в виде числа — используем его."""
    fn = FunPayAdminClient._compute_429_backoff
    assert fn(0, "3", 1.0, 30.0) == 3.0
    assert fn(2, "1", 1.0, 30.0) == 1.0  # явный Retry-After важнее exp
    assert fn(0, "  7  ", 1.0, 30.0) == 7.0  # с whitespace


def test_compute_backoff_retry_after_capped_to_max():
    """Retry-After больше max_seconds → обрезаем до max."""
    fn = FunPayAdminClient._compute_429_backoff
    assert fn(0, "300", 1.0, 30.0) == 30.0
    assert fn(0, "999999", 1.0, 5.0) == 5.0


def test_compute_backoff_invalid_retry_after_falls_back_to_exp():
    """Невалидный Retry-After (HTTP-date или мусор) → fallback на exp."""
    fn = FunPayAdminClient._compute_429_backoff
    # HTTP-date — не парсится → fallback на exp(2)=4
    assert fn(2, "Wed, 21 Oct 2026 07:28:00 GMT", 1.0, 30.0) == 4.0
    # пустая строка → fallback
    assert fn(1, "", 1.0, 30.0) == 2.0
    # мусор → fallback
    assert fn(0, "abc", 1.0, 30.0) == 1.0


def test_compute_backoff_negative_retry_after_falls_back():
    """Отрицательный Retry-After — некорректно, fallback на exp."""
    fn = FunPayAdminClient._compute_429_backoff
    # -5 → fallback на exp(0)=1
    assert fn(0, "-5", 1.0, 30.0) == 1.0


# ─────────────── _sync_get ───────────────


def test_sync_get_succeeds_immediately_no_sleep(monkeypatch):
    """200 OK на первой попытке — ни одного sleep'а."""
    client = _make_client(max_429_retries=3)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)
    monkeypatch.setattr(
        client._session,
        "get",
        lambda *a, **kw: _FakeResponse(status_code=200, body="OK"),
    )

    r = client._sync_get("https://funpay.com/x")

    assert r.status_code == 200
    assert sleeper.sleeps == []


def test_sync_get_retries_on_429_and_succeeds(monkeypatch):
    """429 → 429 → 200: третья попытка успех, два sleep'а согласно exp."""
    client = _make_client(max_429_retries=3, base=0.01, max_cap=1.0)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    responses = iter([
        _FakeResponse(status_code=429, headers={}),
        _FakeResponse(status_code=429, headers={}),
        _FakeResponse(status_code=200, body="OK"),
    ])
    monkeypatch.setattr(client._session, "get", lambda *a, **kw: next(responses))

    r = client._sync_get("https://funpay.com/x")

    assert r.status_code == 200
    # 2 sleep'а перед 2-й и 3-й попытками: 0.01 и 0.02
    assert len(sleeper.sleeps) == 2
    assert sleeper.sleeps[0] == pytest.approx(0.01, rel=0.01)
    assert sleeper.sleeps[1] == pytest.approx(0.02, rel=0.01)


def test_sync_get_uses_retry_after_header(monkeypatch):
    """Если 429-ответ принёс Retry-After=2, мы спим именно 2 (а не 0.01)."""
    client = _make_client(max_429_retries=2, base=0.001, max_cap=10.0)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    responses = iter([
        _FakeResponse(status_code=429, headers={"Retry-After": "2"}),
        _FakeResponse(status_code=200, body="OK"),
    ])
    monkeypatch.setattr(client._session, "get", lambda *a, **kw: next(responses))

    r = client._sync_get("https://funpay.com/x")

    assert r.status_code == 200
    assert sleeper.sleeps == [2.0]


def test_sync_get_429_exhausts_retries_raises_httperror(monkeypatch):
    """Все попытки = 429 → raise_for_status поднимает HTTPError."""
    import requests

    client = _make_client(max_429_retries=2, base=0.001, max_cap=0.01)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    monkeypatch.setattr(
        client._session,
        "get",
        lambda *a, **kw: _FakeResponse(status_code=429, headers={}),
    )

    with pytest.raises(requests.HTTPError):
        client._sync_get("https://funpay.com/x")

    # ровно 2 sleep'а (между 3 попытками)
    assert len(sleeper.sleeps) == 2


def test_sync_get_zero_retries_means_one_attempt(monkeypatch):
    """max_429_retries=0 → 1 попытка, без sleep'ов, 429 сразу падает."""
    import requests

    client = _make_client(max_429_retries=0)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    call_count = {"n": 0}

    def fake_get(*a, **kw):
        call_count["n"] += 1
        return _FakeResponse(status_code=429, headers={})

    monkeypatch.setattr(client._session, "get", fake_get)

    with pytest.raises(requests.HTTPError):
        client._sync_get("https://funpay.com/x")

    assert call_count["n"] == 1
    assert sleeper.sleeps == []


def test_sync_get_network_error_retries(monkeypatch):
    """Сетевая ошибка тоже ретраится exp-backoff'ом, потом успех."""
    import requests

    client = _make_client(max_429_retries=2, base=0.001, max_cap=1.0)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    calls = {"n": 0}

    def fake_get(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("boom")
        return _FakeResponse(status_code=200, body="OK")

    monkeypatch.setattr(client._session, "get", fake_get)

    r = client._sync_get("https://funpay.com/x")

    assert r.status_code == 200
    assert calls["n"] == 2
    assert len(sleeper.sleeps) == 1


# ─────────────── _sync_post_form_with_429_retry ───────────────


def test_sync_post_form_retries_on_429(monkeypatch):
    """POST формы: 429 → 200, возвращает последнюю Response, был sleep."""
    client = _make_client(max_429_retries=2, base=0.005, max_cap=1.0)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    responses = iter([
        _FakeResponse(status_code=429, headers={"Retry-After": "1"}),
        _FakeResponse(status_code=200, body="ok", json_data={"msg": "ok"}),
    ])
    monkeypatch.setattr(client._session, "post", lambda *a, **kw: next(responses))

    r = client._sync_post_form_with_429_retry(
        "https://funpay.com/lots/offerSave", {"offer_id": "1"}
    )

    assert r.status_code == 200
    assert sleeper.sleeps == [1.0]


def test_sync_post_form_returns_last_429_on_exhaust(monkeypatch):
    """POST формы: все 429 → возвращает последнюю Response (не raise)."""
    client = _make_client(max_429_retries=1, base=0.001, max_cap=0.01)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    monkeypatch.setattr(
        client._session,
        "post",
        lambda *a, **kw: _FakeResponse(status_code=429, headers={}),
    )

    r = client._sync_post_form_with_429_retry("https://funpay.com/x", {})

    # raises НЕТ — POST-обёртка отдаёт последнюю Response (даже 429),
    # чтобы вызывающий мог нарисовать понятный funpay_error
    assert r.status_code == 429
    assert len(sleeper.sleeps) == 1  # ровно 1 sleep между 2 попытками


# ─────────────── save_lot (интеграция) ───────────────


def test_save_lot_recovers_after_429(monkeypatch):
    """save_lot: 429 → 200(json msg=ok) → result['ok']=True.
    Без новой обёртки этот сценарий валится 'funpay_error: HTTP 429'."""
    client = _make_client(max_429_retries=2, base=0.001, max_cap=1.0)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    responses = iter([
        _FakeResponse(status_code=429, headers={}),
        _FakeResponse(
            status_code=200,
            body='{"msg":"ok"}',
            json_data={"msg": "ok"},
            headers={"Content-Type": "application/json"},
        ),
    ])
    monkeypatch.setattr(client._session, "post", lambda *a, **kw: next(responses))

    lot = LotFields(lot_id=12345, node_id=99, raw_fields={"price": "100.00"})
    result = asyncio.run(client.save_lot(lot))

    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result.get("json") == {"msg": "ok"}


def test_save_lot_429_after_all_retries_returns_clear_error(monkeypatch):
    """save_lot: 429 везде → ok=False, funpay_error явно про 429
    (а не 'Получили HTML, не JSON'). Это сильно лучше для дебага."""
    client = _make_client(max_429_retries=1, base=0.001, max_cap=0.01)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    monkeypatch.setattr(
        client._session,
        "post",
        lambda *a, **kw: _FakeResponse(
            status_code=429,
            body="<html>too many</html>",
            headers={"Retry-After": "60"},
        ),
    )

    lot = LotFields(lot_id=42, node_id=1, raw_fields={"price": "100.00"})
    result = asyncio.run(client.save_lot(lot))

    assert result["ok"] is False
    assert result["http_status"] == 429
    err = str(result.get("funpay_error", ""))
    assert "429" in err
    assert "rate-limit" in err.lower() or "Retry-After" in err
    # body_preview не утекает HTML дальше — он есть, но funpay_error чистый
    assert "<html>" not in err


def test_save_lot_200_with_error_msg_returns_funpay_error(monkeypatch):
    """save_lot: 200 + json {'msg':'некий текст ошибки'} → ok=False,
    funpay_error == текст ошибки. Этот код не должен сломаться после
    добавления 429-обёртки."""
    client = _make_client(max_429_retries=0)
    monkeypatch.setattr("time.sleep", _SleepRecorder())

    monkeypatch.setattr(
        client._session,
        "post",
        lambda *a, **kw: _FakeResponse(
            status_code=200,
            body='{"msg":"Лот не найден"}',
            json_data={"msg": "Лот не найден"},
            headers={"Content-Type": "application/json"},
        ),
    )

    lot = LotFields(lot_id=42, node_id=1, raw_fields={"price": "100.00"})
    result = asyncio.run(client.save_lot(lot))

    assert result["ok"] is False
    assert result["http_status"] == 200
    assert result["funpay_error"] == "Лот не найден"


# ─────────────── __init__ настройки ───────────────


def test_init_clamps_negative_retries():
    """__init__: отрицательные retries → 0 (не валимся, но не -1)."""
    client = FunPayAdminClient(
        golden_key="x", max_429_retries=-5, base_429_backoff_seconds=0.1
    )
    assert client._max_429_retries == 0


def test_init_max_backoff_at_least_base():
    """max_backoff не может быть меньше base (это бы дало неуменьшающийся exp).
    Если пользователь так задал — поднимаем max до base."""
    client = FunPayAdminClient(
        golden_key="x",
        base_429_backoff_seconds=10.0,
        max_429_backoff_seconds=1.0,  # абсурдно мало
    )
    assert client._max_429_backoff >= client._base_429_backoff


# ─────────────── _sync_get на 5xx (transient gateway) ───────────────


def test_sync_get_retries_on_502(monkeypatch):
    """502 → 502 → 200: ретраит до успеха, два sleep'а."""
    client = _make_client(max_429_retries=0, max_5xx_retries=3, base=0.01)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    responses = iter([
        _FakeResponse(status_code=502, body="<html>502 Bad Gateway</html>"),
        _FakeResponse(status_code=502, body="<html>502 Bad Gateway</html>"),
        _FakeResponse(status_code=200, body="OK"),
    ])
    monkeypatch.setattr(client._session, "get", lambda *a, **kw: next(responses))

    r = client._sync_get("https://funpay.com/lots/offerEdit?offer=1")

    assert r.status_code == 200
    assert len(sleeper.sleeps) == 2
    # exp с base=0.01: первая пауза 0.01, вторая 0.02
    assert sleeper.sleeps[0] == pytest.approx(0.01, rel=0.01)
    assert sleeper.sleeps[1] == pytest.approx(0.02, rel=0.01)


def test_sync_get_retries_on_503_and_504(monkeypatch):
    """503 и 504 тоже ретраятся как transient."""
    client = _make_client(max_429_retries=0, max_5xx_retries=2)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    for status in (503, 504):
        sleeper.sleeps.clear()
        responses = iter([
            _FakeResponse(status_code=status),
            _FakeResponse(status_code=200, body="ok"),
        ])
        monkeypatch.setattr(client._session, "get", lambda *a, **kw: next(responses))

        r = client._sync_get("https://funpay.com/x")
        assert r.status_code == 200, f"должно ретраиться на {status}"
        assert len(sleeper.sleeps) == 1


def test_sync_get_5xx_uses_retry_after_header(monkeypatch):
    """Если 5xx-ответ принёс Retry-After=2 — спим именно 2с (а не exp)."""
    client = _make_client(max_429_retries=0, max_5xx_retries=2, base=0.001, max_cap=10.0)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    responses = iter([
        _FakeResponse(status_code=503, headers={"Retry-After": "2"}),
        _FakeResponse(status_code=200, body="ok"),
    ])
    monkeypatch.setattr(client._session, "get", lambda *a, **kw: next(responses))

    r = client._sync_get("https://funpay.com/x")
    assert r.status_code == 200
    assert sleeper.sleeps == [2.0]


def test_sync_get_does_not_retry_500(monkeypatch):
    """500 (application bug у FunPay) — НЕ ретраим, повтор бесполезен.
    Это критически важно: иначе на каждый битый лот мы будем тратить
    3 запроса вместо 1, разогревая FunPay-rate-limit."""
    import requests

    client = _make_client(max_429_retries=3, max_5xx_retries=3)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    call_count = {"n": 0}

    def fake_get(*a, **kw):
        call_count["n"] += 1
        return _FakeResponse(status_code=500, body="Internal Server Error")

    monkeypatch.setattr(client._session, "get", fake_get)

    with pytest.raises(requests.HTTPError):
        client._sync_get("https://funpay.com/x")

    assert call_count["n"] == 1, "500 должен НЕ ретраиться"
    assert sleeper.sleeps == []


def test_sync_get_does_not_retry_4xx(monkeypatch):
    """404/401/403 — тоже не ретраим (это наши проблемы, не FunPay'a)."""
    import requests

    client = _make_client(max_429_retries=3, max_5xx_retries=3)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    for status in (401, 403, 404):
        call_count = {"n": 0}

        def fake_get(*a, _s=status, **kw):
            call_count["n"] += 1
            return _FakeResponse(status_code=_s, body=f"http {_s}")

        monkeypatch.setattr(client._session, "get", fake_get)
        sleeper.sleeps.clear()

        with pytest.raises(requests.HTTPError):
            client._sync_get("https://funpay.com/x")

        assert call_count["n"] == 1, f"{status} не должен ретраиться"
        assert sleeper.sleeps == []


def test_sync_get_5xx_exhausts_retries_raises_httperror(monkeypatch):
    """Все попытки = 502 → raise_for_status поднимает HTTPError."""
    import requests

    client = _make_client(max_429_retries=0, max_5xx_retries=2, base=0.001, max_cap=0.01)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    monkeypatch.setattr(
        client._session,
        "get",
        lambda *a, **kw: _FakeResponse(status_code=502),
    )

    with pytest.raises(requests.HTTPError):
        client._sync_get("https://funpay.com/x")

    # 2 retries → 3 попытки → 2 sleep'а между ними
    assert len(sleeper.sleeps) == 2


def test_sync_get_5xx_zero_retries_means_one_attempt(monkeypatch):
    """max_5xx_retries=0 → 1 попытка, 502 сразу падает без sleep'ов."""
    import requests

    client = _make_client(max_429_retries=0, max_5xx_retries=0)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    call_count = {"n": 0}

    def fake_get(*a, **kw):
        call_count["n"] += 1
        return _FakeResponse(status_code=502)

    monkeypatch.setattr(client._session, "get", fake_get)

    with pytest.raises(requests.HTTPError):
        client._sync_get("https://funpay.com/x")

    assert call_count["n"] == 1
    assert sleeper.sleeps == []


def test_sync_get_429_and_5xx_counters_are_independent(monkeypatch):
    """Если сервер чередует 429 и 502, оба счётчика тратятся независимо.
    Это сохраняет шанс на успех при «двойной» нестабильности."""
    client = _make_client(max_429_retries=2, max_5xx_retries=2, base=0.001)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    responses = iter([
        _FakeResponse(status_code=429),                    # 429-counter: 1/2
        _FakeResponse(status_code=502),                    # 5xx-counter: 1/2
        _FakeResponse(status_code=429),                    # 429-counter: 2/2
        _FakeResponse(status_code=502),                    # 5xx-counter: 2/2
        _FakeResponse(status_code=200, body="finally"),    # успех
    ])
    monkeypatch.setattr(client._session, "get", lambda *a, **kw: next(responses))

    r = client._sync_get("https://funpay.com/x")
    assert r.status_code == 200
    # 4 sleep'а между 5 запросами
    assert len(sleeper.sleeps) == 4


def test_sync_get_network_error_uses_5xx_counter(monkeypatch):
    """ConnectionError должен тратить 5xx-счётчик, а не 429."""
    import requests

    client = _make_client(max_429_retries=0, max_5xx_retries=2, base=0.001)
    sleeper = _SleepRecorder()
    monkeypatch.setattr("time.sleep", sleeper)

    calls = {"n": 0}

    def fake_get(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("network down")
        return _FakeResponse(status_code=200, body="OK")

    monkeypatch.setattr(client._session, "get", fake_get)

    r = client._sync_get("https://funpay.com/x")

    assert r.status_code == 200
    assert calls["n"] == 2
    assert len(sleeper.sleeps) == 1
    # И при этом max_429_retries=0 — это значит сетевая ошибка НЕ съела
    # 429-счётчик. Проверка через противопоставление: сразу за этим
    # тестом 429 ответ должен иметь ноль ретраев → один запрос → fail.


def test_sync_get_logs_5xx_status_in_warning(monkeypatch, caplog):
    """Лог-сообщение должно содержать конкретный код (502/503/504),
    а не общий '5xx', чтобы в проде по логу было сразу видно, что упало."""
    import logging

    client = _make_client(max_429_retries=0, max_5xx_retries=1, base=0.001)
    monkeypatch.setattr("time.sleep", _SleepRecorder())

    responses = iter([
        _FakeResponse(status_code=502),
        _FakeResponse(status_code=200, body="OK"),
    ])
    monkeypatch.setattr(client._session, "get", lambda *a, **kw: next(responses))

    # Подцепляем loguru → caplog (loguru пишет в собственный logger,
    # но мы можем грубо отловить через add handler).
    import io
    from loguru import logger as loguru_logger

    stream = io.StringIO()
    handler_id = loguru_logger.add(stream, level="WARNING", format="{message}")
    try:
        client._sync_get("https://funpay.com/lots/offerEdit?offer=42")
    finally:
        loguru_logger.remove(handler_id)

    log_output = stream.getvalue()
    assert "502" in log_output, f"warning должен упомянуть код 502, got: {log_output!r}"


# ─────────────── интеграция с __init__ ───────────────


def test_init_max_5xx_retries_clamped_to_zero():
    """Отрицательные значения 5xx-retries → 0."""
    client = FunPayAdminClient(
        golden_key="x", max_5xx_retries=-3,
    )
    assert client._max_5xx_retries == 0


def test_init_defaults_for_5xx_retries():
    """Дефолтное значение — 2 (см. конфиг)."""
    client = FunPayAdminClient(golden_key="x")
    assert client._max_5xx_retries == 2
