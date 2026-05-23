"""
Тесты для глобального rate-limiter'а FunPay (_RateLimiter).

Проверяем:
  * min_interval соблюдается между sequential calls;
  * max_concurrent ограничивает параллелизм (через ThreadPoolExecutor);
  * semaphore отпускается ДАЖЕ при исключении внутри блока;
  * clamping: max_concurrent=0 → fallback в 1 (защита от deadlock'а);
  * интеграция с _sync_get: rate-limiter реально берётся вокруг session.get;
  * интеграция с _sync_post: rate-limiter реально берётся вокруг session.post.

Времена в тестах маленькие (10-50ms), чтобы CI бежал быстро.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.funpay.admin_http import FunPayAdminClient, _RateLimiter


# ─────────────── unit: _RateLimiter ───────────────


def test_rate_limiter_min_interval_sequential():
    """Два последовательных acquire должны быть разделены ≥ min_interval."""
    lim = _RateLimiter(max_concurrent=4, min_interval_seconds=0.08)

    t1 = time.monotonic()
    with lim.acquire():
        pass
    t2 = time.monotonic()
    with lim.acquire():
        pass
    t3 = time.monotonic()

    # Между ВТОРЫМ acquire и первым — пауза ≥ 65ms (запас на jitter).
    assert (t3 - t2) >= 0.065, f"min_interval не соблюдён: {(t3 - t2) * 1000:.1f}ms"
    # Первый acquire — мгновенный (last_at = 0 → wait отрицательный → не спим).
    assert (t2 - t1) < 0.03, f"первый acquire должен быть мгновенным"


def test_rate_limiter_min_interval_zero_no_sleep():
    """min_interval=0 → НЕТ задержки между запросами."""
    lim = _RateLimiter(max_concurrent=4, min_interval_seconds=0.0)

    start = time.monotonic()
    for _ in range(10):
        with lim.acquire():
            pass
    elapsed = time.monotonic() - start

    # 10 acquire без задержки = почти мгновенно (< 50ms на любой машине).
    assert elapsed < 0.1, f"10 acquire без interval'а должны быть быстрыми: {elapsed:.3f}s"


def test_rate_limiter_min_interval_multiple_steps():
    """5 запросов с interval=30ms должны занять ≥ 100ms (4 паузы)."""
    lim = _RateLimiter(max_concurrent=4, min_interval_seconds=0.03)

    start = time.monotonic()
    for _ in range(5):
        with lim.acquire():
            pass
    elapsed = time.monotonic() - start

    # 4 интервала по 30ms = 120ms ideal; 100ms — запас на time.sleep jitter
    # (Windows иногда округляет sleep вниз для коротких интервалов).
    assert elapsed >= 0.1, f"ожидалось ≥100ms, получили {elapsed * 1000:.1f}ms"


def test_rate_limiter_concurrent_limit_blocks_extra_threads():
    """max_concurrent=2: при 5 параллельных acquire одновременно работают только 2."""
    lim = _RateLimiter(max_concurrent=2, min_interval_seconds=0.0)
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def worker():
        nonlocal in_flight, max_in_flight
        with lim.acquire():
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)  # удерживаем слот, чтобы прижать остальных
            with lock:
                in_flight -= 1

    with ThreadPoolExecutor(max_workers=5) as ex:
        for _ in range(5):
            ex.submit(worker)

    assert max_in_flight == 2, f"max_concurrent=2 нарушен: было {max_in_flight}"


def test_rate_limiter_releases_semaphore_on_exception():
    """Если внутри acquire-блока вылетело исключение — слот должен
    освободиться. Иначе через несколько таких exception'ов лимитер
    заполнится насовсем (deadlock на дальнейших acquire)."""
    lim = _RateLimiter(max_concurrent=1, min_interval_seconds=0.0)

    # Каждый раз падаем — но семафор должен освобождаться.
    for _ in range(5):
        try:
            with lim.acquire():
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    # После 5 неудачных acquire слот всё ещё свободен.
    acquired = lim._sem.acquire(blocking=False)
    assert acquired, "semaphore не освобождён после exception в acquire-блоке"
    lim._sem.release()


def test_rate_limiter_clamps_zero_concurrent_to_one():
    """max_concurrent=0 — это был бы deadlock на первом же acquire.
    Защищаемся clamping'ом в минимум 1."""
    lim = _RateLimiter(max_concurrent=0, min_interval_seconds=0.0)
    # Если бы было 0 — следующая строка зависла бы навсегда. Если clamped в 1 —
    # acquire-release пройдёт мгновенно.
    with lim.acquire():
        pass


def test_rate_limiter_clamps_negative_concurrent_to_one():
    lim = _RateLimiter(max_concurrent=-5, min_interval_seconds=0.0)
    with lim.acquire():
        pass


def test_rate_limiter_clamps_negative_interval_to_zero():
    """Отрицательный interval не должен сломать логику."""
    lim = _RateLimiter(max_concurrent=2, min_interval_seconds=-1.0)
    start = time.monotonic()
    for _ in range(3):
        with lim.acquire():
            pass
    elapsed = time.monotonic() - start
    # Отсутствие interval'а = быстро.
    assert elapsed < 0.05


def test_rate_limiter_concurrent_and_interval_combine():
    """concurrent=2 + interval=30ms: при 4 параллельных воркерах
    в каждый момент работает ≤2, при этом стартуют они с шагом ≥interval.

    Интервал 30ms взят сознательно: time.monotonic()/time.sleep() на
    Windows имеют шаг ~15ms (system timer), поэтому слишком короткие
    интервалы (≤10ms) «съедаются» округлением и тест flaky.
    """
    lim = _RateLimiter(max_concurrent=2, min_interval_seconds=0.03)
    start_times: list[float] = []
    lock = threading.Lock()

    def worker():
        with lim.acquire():
            with lock:
                start_times.append(time.monotonic())
            time.sleep(0.05)  # удерживаем слот, чтобы concurrent сработал

    with ThreadPoolExecutor(max_workers=4) as ex:
        for _ in range(4):
            ex.submit(worker)

    assert len(start_times) == 4
    start_times.sort()
    # Между соседними стартами — минимум ~interval (с запасом на jitter).
    for i in range(1, len(start_times)):
        gap = start_times[i] - start_times[i - 1]
        assert gap >= 0.015, f"стартов {i-1} и {i}: gap={gap * 1000:.1f}ms"


# ─────────────── интеграция с _sync_get ───────────────


class _FakeOkResponse:
    """Минимальный mock для requests.Response: только status_code/raise_for_status."""
    status_code = 200
    text = "OK"
    content = b"OK"
    headers: dict[str, str] = {}

    def raise_for_status(self):
        return None


def _make_client_with_rate(
    *,
    max_concurrent: int = 2,
    min_interval: float = 0.0,
) -> FunPayAdminClient:
    return FunPayAdminClient(
        golden_key="dummy",
        rate_max_concurrent=max_concurrent,
        rate_min_interval_seconds=min_interval,
        # выключаем backoff'ы — тесты только про rate-limit
        max_429_retries=0,
        max_5xx_retries=0,
    )


def test_sync_get_uses_rate_limiter_for_concurrency(monkeypatch):
    """5 параллельных _sync_get с concurrent=2: max in-flight = 2."""
    client = _make_client_with_rate(max_concurrent=2)
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def fake_get(url, *a, **kw):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.04)
        with lock:
            in_flight -= 1
        return _FakeOkResponse()

    monkeypatch.setattr(client._session, "get", fake_get)

    def call():
        return client._sync_get("https://funpay.com/x")

    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(lambda _: call(), range(5)))

    assert max_in_flight == 2, f"_sync_get обошёл rate-limiter: {max_in_flight}"


def test_sync_get_uses_min_interval(monkeypatch):
    """3 последовательных _sync_get с interval=50ms = ≥80ms суммарно.

    Берём interval побольше (50ms) — time.sleep на Windows для очень
    коротких интервалов может округлять вниз, и тест становится flaky.
    Запас от 100ms к 80ms покрывает обычный sleep-jitter."""
    client = _make_client_with_rate(max_concurrent=4, min_interval=0.05)

    monkeypatch.setattr(client._session, "get", lambda *a, **kw: _FakeOkResponse())

    start = time.monotonic()
    for _ in range(3):
        client._sync_get("https://funpay.com/x")
    elapsed = time.monotonic() - start

    # 2 интервала по 50ms = 100ms ideal; 80ms — запас на платформенный jitter.
    assert elapsed >= 0.08, f"min_interval не сработал: {elapsed * 1000:.1f}ms"


def test_sync_post_uses_rate_limiter(monkeypatch):
    """POST тоже под лимитером (FunPay считает общий RPS — GET+POST)."""
    client = _make_client_with_rate(max_concurrent=2)
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def fake_post(url, *a, **kw):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.04)
        with lock:
            in_flight -= 1
        return _FakeOkResponse()

    monkeypatch.setattr(client._session, "post", fake_post)

    def call():
        return client._sync_post("https://funpay.com/lots/offerSave", {"k": "v"})

    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(lambda _: call(), range(5)))

    assert max_in_flight == 2, f"_sync_post обошёл rate-limiter: {max_in_flight}"


def test_sync_get_and_sync_post_share_one_limiter(monkeypatch):
    """GET и POST используют ОДИН лимитер: при concurrent=1 нельзя
    одновременно делать GET и POST."""
    client = _make_client_with_rate(max_concurrent=1)
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def fake_get(*a, **kw):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.03)
        with lock:
            in_flight -= 1
        return _FakeOkResponse()

    def fake_post(*a, **kw):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.03)
        with lock:
            in_flight -= 1
        return _FakeOkResponse()

    monkeypatch.setattr(client._session, "get", fake_get)
    monkeypatch.setattr(client._session, "post", fake_post)

    with ThreadPoolExecutor(max_workers=4) as ex:
        ex.submit(client._sync_get, "https://funpay.com/x")
        ex.submit(client._sync_post, "https://funpay.com/y", {})
        ex.submit(client._sync_get, "https://funpay.com/z")
        ex.submit(client._sync_post, "https://funpay.com/w", {})

    assert max_in_flight == 1, f"GET+POST должны делить лимитер: {max_in_flight}"


def test_sync_get_releases_slot_on_http_exception(monkeypatch):
    """Если session.get кинул исключение, слот всё равно должен освобождаться.
    Иначе после первого же fail'а лимитер «потеряет» слот навсегда."""
    import requests

    client = _make_client_with_rate(max_concurrent=1, min_interval=0.0)

    raise_n = [3]  # первые 3 вызова бросают ConnectionError (исчерпание ретраев)

    def fake_get(*a, **kw):
        if raise_n[0] > 0:
            raise_n[0] -= 1
            raise requests.ConnectionError("network down")
        return _FakeOkResponse()

    monkeypatch.setattr(client._session, "get", fake_get)

    # max_5xx_retries=0 → один шанс → сразу ConnectionError
    with pytest.raises(requests.ConnectionError):
        client._sync_get("https://funpay.com/x")

    # Слот не должен быть «потерян»: следующий _sync_get берёт лимитер
    # и проходит. Если бы acquire не релизился — мы бы зависли тут
    # навсегда. На случай регрессии — taймаут через threading:
    done = threading.Event()
    result_holder: list = []

    def try_call():
        try:
            result_holder.append(client._sync_get("https://funpay.com/x"))
        except Exception as e:  # noqa: BLE001
            result_holder.append(e)
        finally:
            done.set()

    threading.Thread(target=try_call, daemon=True).start()
    assert done.wait(timeout=2.0), "слот не освобождён после exception → deadlock"


# ─────────────── интеграция с FunPayAdminClient.__init__ ───────────────


def test_admin_client_creates_rate_limiter_by_default():
    client = FunPayAdminClient(golden_key="x")
    assert isinstance(client._rate_limiter, _RateLimiter)
    # Дефолты совпадают с настройками в config.py.
    assert client._rate_limiter._sem._initial_value == 4
    assert client._rate_limiter._interval == pytest.approx(0.1)


def test_admin_client_passes_rate_settings():
    client = FunPayAdminClient(
        golden_key="x",
        rate_max_concurrent=7,
        rate_min_interval_seconds=0.25,
    )
    assert client._rate_limiter._sem._initial_value == 7
    assert client._rate_limiter._interval == pytest.approx(0.25)
