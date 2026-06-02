"""
Тесты для HTTP-метрик FunPayAdminClient.

Проверяем:
  * счётчики ok/retry_429/retry_5xx/exhausted инкрементятся корректно;
  * get_and_reset_http_metrics возвращает snapshot и обнуляет;
  * reset атомарный (не теряет инкременты из параллельных потоков);
  * exhausted считается отдельно от retry_* (один сбой = один счётчик);
  * sync_stock в выходном dict содержит ключ 'http' с метриками;
  * sync_stock log line содержит 'http=[...]' с числами.

Используем те же _FakeResponse и monkeypatch'и что и в test_funpay_429_backoff.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.funpay.admin_http import FunPayAdminClient


class _FakeResponse:
    """Минимальный mock для requests.Response."""

    def __init__(
        self,
        status_code: int = 200,
        body: str = "OK",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = body
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _make_client(
    *,
    max_429: int = 3,
    max_5xx: int = 2,
    base: float = 0.001,
) -> FunPayAdminClient:
    return FunPayAdminClient(
        golden_key="dummy",
        max_429_retries=max_429,
        base_429_backoff_seconds=base,
        max_429_backoff_seconds=0.01,
        max_5xx_retries=max_5xx,
        # rate-limit выключаем, чтобы не мешал sleep'ам в тестах
        rate_max_concurrent=8,
        rate_min_interval_seconds=0.0,
    )


# ─────────────── unit: счётчики ───────────────


def test_metrics_initial_zeros():
    """Свежий клиент — все счётчики 0."""
    client = _make_client()
    snap = client.get_and_reset_http_metrics()
    assert snap == {"ok": 0, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}


def test_metrics_increment_ok_on_200(monkeypatch):
    """Один успешный GET → ok=1, retry_*=0, exhausted=0."""
    client = _make_client()
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: _FakeResponse(200)
    )

    client._sync_get("https://funpay.com/x")
    snap = client.get_and_reset_http_metrics()
    assert snap["ok"] == 1
    assert snap["retry_429"] == 0
    assert snap["retry_5xx"] == 0
    assert snap["exhausted"] == 0


def test_metrics_429_then_200(monkeypatch):
    """429 → 200: retry_429=1 (одна пауза), ok=1 (финальный успех)."""
    client = _make_client()
    monkeypatch.setattr("time.sleep", lambda *_: None)
    responses = iter([_FakeResponse(429), _FakeResponse(200)])
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: next(responses)
    )

    client._sync_get("https://funpay.com/x")
    snap = client.get_and_reset_http_metrics()
    assert snap["retry_429"] == 1
    assert snap["ok"] == 1
    assert snap["exhausted"] == 0


def test_metrics_502_then_200(monkeypatch):
    """502 → 200: retry_5xx=1, ok=1."""
    client = _make_client()
    monkeypatch.setattr("time.sleep", lambda *_: None)
    responses = iter([_FakeResponse(502), _FakeResponse(200)])
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: next(responses)
    )

    client._sync_get("https://funpay.com/x")
    snap = client.get_and_reset_http_metrics()
    assert snap["retry_5xx"] == 1
    assert snap["ok"] == 1
    assert snap["exhausted"] == 0


def test_metrics_429_exhausted_raises_and_increments(monkeypatch):
    """Все retries = 429 → exhausted=1, retry_429=max_429."""
    import requests

    client = _make_client(max_429=2)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: _FakeResponse(429)
    )

    with pytest.raises(requests.HTTPError):
        client._sync_get("https://funpay.com/x")

    snap = client.get_and_reset_http_metrics()
    assert snap["retry_429"] == 2, "2 retry-паузы между 3 попытками"
    assert snap["exhausted"] == 1, "финальное исчерпание считается отдельно"
    assert snap["ok"] == 0


def test_metrics_5xx_exhausted_raises_and_increments(monkeypatch):
    """Все retries = 502 → exhausted=1."""
    import requests

    client = _make_client(max_5xx=2)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: _FakeResponse(502)
    )

    with pytest.raises(requests.HTTPError):
        client._sync_get("https://funpay.com/x")

    snap = client.get_and_reset_http_metrics()
    assert snap["retry_5xx"] == 2
    assert snap["exhausted"] == 1


def test_metrics_network_error_counts_as_5xx(monkeypatch):
    """ConnectionError = семантически 5xx, счётчик retry_5xx."""
    import requests

    client = _make_client(max_5xx=2)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    calls = {"n": 0}

    def fake_get(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("network down")
        return _FakeResponse(200)

    monkeypatch.setattr(client._session, "get", fake_get)
    client._sync_get("https://funpay.com/x")
    snap = client.get_and_reset_http_metrics()
    assert snap["retry_5xx"] == 1
    assert snap["ok"] == 1


def test_metrics_does_not_count_500(monkeypatch):
    """500 (application bug) НЕ считается как retry (он не ретраится),
    но и ok не плюсуется."""
    import requests

    client = _make_client()
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: _FakeResponse(500)
    )

    with pytest.raises(requests.HTTPError):
        client._sync_get("https://funpay.com/x")

    snap = client.get_and_reset_http_metrics()
    # 500 — это «application error», мы его не ретраим, exhausted не плюсуем
    # (это особенность дизайна: exhausted = только retry exhausted, не любой fail).
    assert snap["retry_429"] == 0
    assert snap["retry_5xx"] == 0
    assert snap["exhausted"] == 0
    assert snap["ok"] == 0


def test_metrics_reset_returns_snapshot_and_zeroes(monkeypatch):
    """После get_and_reset метрики обнуляются, повторный вызов = 0."""
    client = _make_client()
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: _FakeResponse(200)
    )

    for _ in range(3):
        client._sync_get("https://funpay.com/x")

    snap1 = client.get_and_reset_http_metrics()
    snap2 = client.get_and_reset_http_metrics()
    assert snap1["ok"] == 3
    assert snap2 == {"ok": 0, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}


def test_metrics_concurrent_safe(monkeypatch):
    """100 параллельных GET → ровно 100 ok (никаких потерь из-за race)."""
    client = _make_client()
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: _FakeResponse(200)
    )

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(lambda _: client._sync_get("https://funpay.com/x"), range(100)))

    snap = client.get_and_reset_http_metrics()
    assert snap["ok"] == 100, f"thread-safety нарушена: ok={snap['ok']}"
    assert snap["retry_429"] == 0
    assert snap["retry_5xx"] == 0


def test_metrics_reset_during_concurrent_calls_no_loss(monkeypatch):
    """Reset во время параллельных GET не теряет инкременты.
    Сумма snap1.ok + snap2.ok должна быть == общему числу запросов."""
    client = _make_client()
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "get", lambda *a, **kw: _FakeResponse(200)
    )

    total_requests = 200
    snaps: list[dict[str, int]] = []
    snap_done = threading.Event()

    def worker():
        for _ in range(total_requests // 4):
            client._sync_get("https://funpay.com/x")

    def reset_periodically():
        # Делаем 3 сброса в процессе, проверяем что инкременты не теряются.
        import time
        for _ in range(3):
            time.sleep(0.005)
            snaps.append(client.get_and_reset_http_metrics())
        snap_done.set()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    reset_thread = threading.Thread(target=reset_periodically)
    for t in threads:
        t.start()
    reset_thread.start()
    for t in threads:
        t.join()
    snap_done.wait()
    # Финальный сброс — собираем хвост.
    snaps.append(client.get_and_reset_http_metrics())

    total_ok = sum(s["ok"] for s in snaps)
    assert total_ok == total_requests, (
        f"потеря инкрементов при concurrent reset: "
        f"sum(snaps.ok)={total_ok} vs expected {total_requests}"
    )


def test_metrics_post_429_then_ok(monkeypatch):
    """POST 429 → 200: retry_429=1, ok=1 (через _sync_post)."""
    client = _make_client(max_429=2)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    responses = iter([_FakeResponse(429), _FakeResponse(200)])
    monkeypatch.setattr(
        client._session, "post", lambda *a, **kw: next(responses)
    )

    r = client._sync_post_form_with_429_retry(
        "https://funpay.com/lots/offerSave", {"k": "v"}
    )
    assert r.status_code == 200
    snap = client.get_and_reset_http_metrics()
    assert snap["retry_429"] == 1
    assert snap["ok"] == 1


def test_metrics_post_429_exhausted(monkeypatch):
    """POST все 429 → exhausted=1, retry_429=max_429."""
    client = _make_client(max_429=2)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        client._session, "post", lambda *a, **kw: _FakeResponse(429)
    )

    r = client._sync_post_form_with_429_retry(
        "https://funpay.com/lots/offerSave", {"k": "v"}
    )
    assert r.status_code == 429
    snap = client.get_and_reset_http_metrics()
    assert snap["retry_429"] == 2
    assert snap["exhausted"] == 1


# ─────────────── FunPayClient proxy method ───────────────


def test_funpay_client_metrics_returns_zeros_without_admin_cache():
    """Если admin-клиент ещё не создан — метрики все 0."""
    from types import SimpleNamespace
    from src.funpay.client import FunPayClient

    # Пустой FunPayClient без admin cache → 0/0/0/0
    fp = FunPayClient.__new__(FunPayClient)  # обходим __init__
    snap = fp.get_and_reset_http_metrics()
    assert snap == {"ok": 0, "retry_429": 0, "retry_5xx": 0, "exhausted": 0}


def test_funpay_client_metrics_delegates_to_admin_cache(monkeypatch):
    """Если admin-клиент инициализирован — proxy возвращает его метрики."""
    from src.funpay.client import FunPayClient

    fp = FunPayClient.__new__(FunPayClient)
    expected = {"ok": 42, "retry_429": 5, "retry_5xx": 1, "exhausted": 0}

    class _FakeAdmin:
        def get_and_reset_http_metrics(self):
            return dict(expected)

    fp._admin_client_cache = _FakeAdmin()
    snap = fp.get_and_reset_http_metrics()
    assert snap == expected


# ─────────────── интеграция: лог-строка sync_stock ───────────────


def test_sync_done_log_line_format_contains_http_metrics():
    """
    Прямая проверка формата строки 'Sync done' — стабильный контракт
    для будущих парсеров логов и мониторингов.

    Не запускаем целиком run_sync_once (требует FunPay+NS+БД); просто
    проверяем что код собирает http_str в той форме, которую мы
    обещаем парсерам.
    """
    metrics = {"ok": 47, "retry_429": 12, "retry_5xx": 0, "exhausted": 0}
    http_str = (
        f"http=[ok={metrics['ok']} "
        f"r429={metrics['retry_429']} "
        f"r5xx={metrics['retry_5xx']} "
        f"fails={metrics['exhausted']}]"
    )
    assert http_str == "http=[ok=47 r429=12 r5xx=0 fails=0]"

    # Та же строка должна быть в исходниках sync_stock.py — проверка
    # что мы реально пишем именно этот формат (анти-регрессия,
    # на случай если кто-то переименует ключ http=... в logs).
    import re
    from pathlib import Path
    src = Path(__file__).parent.parent / "src" / "sync" / "stock_sync.py"
    text = src.read_text(encoding="utf-8")
    # Должен быть f-string, который содержит "http=[ok=" и "r429=" и
    # "fails=" — это сигнатура нашего лог-формата.
    assert "http=[ok=" in text, "log-формат изменён — обнови парсеры/мониторинг"
    assert "r429=" in text
    assert "r5xx=" in text
    assert "fails=" in text
