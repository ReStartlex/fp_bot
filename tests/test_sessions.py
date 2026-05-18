"""Тесты для in-memory pagination store и хелпера paginate()."""
from __future__ import annotations

import time

import pytest

from src.alerts.sessions import PAGE_SIZE, PaginationStore, paginate


def test_paginate_empty():
    items, page, total = paginate([], 0)
    assert items == []
    assert page == 0
    assert total == 0


def test_paginate_one_page():
    data = list(range(5))
    items, page, total = paginate(data, 0)
    assert items == [0, 1, 2, 3, 4]
    assert page == 0
    assert total == 1


def test_paginate_multiple_pages():
    data = list(range(25))  # 25 items, page_size=10 -> 3 страницы
    p0, page0, total = paginate(data, 0)
    p1, page1, _ = paginate(data, 1)
    p2, page2, _ = paginate(data, 2)
    assert page0 == 0 and page1 == 1 and page2 == 2
    assert total == 3
    assert p0 == list(range(0, 10))
    assert p1 == list(range(10, 20))
    assert p2 == [20, 21, 22, 23, 24]


def test_paginate_clamps_out_of_range():
    data = list(range(15))
    items, page, total = paginate(data, 99)
    # 15 / 10 -> 2 страницы (0, 1), 99 должен скоррэктиться к последней
    assert page == 1
    assert total == 2
    assert items == [10, 11, 12, 13, 14]


def test_paginate_negative_page_clamped():
    data = list(range(5))
    items, page, total = paginate(data, -5)
    assert page == 0
    assert items == data
    assert total == 1


def test_paginate_custom_page_size():
    data = list(range(7))
    items, _, total = paginate(data, 0, page_size=3)
    assert items == [0, 1, 2]
    assert total == 3


def test_store_put_and_get():
    store = PaginationStore()
    sid = store.put([1, 2, 3], title="numbers", meta={"q": "x"})
    assert isinstance(sid, str)
    assert len(sid) == 8  # 4 байта hex
    sess = store.get(sid)
    assert sess is not None
    assert sess.items == [1, 2, 3]
    assert sess.title == "numbers"
    assert sess.meta == {"q": "x"}


def test_store_get_missing_returns_none():
    store = PaginationStore()
    assert store.get("deadbeef") is None


def test_store_ttl_expires():
    store = PaginationStore(ttl_seconds=1)
    sid = store.put([1, 2, 3])
    assert store.get(sid) is not None
    # Подделываем timestamp в прошлое
    store._data[sid].created_at = time.time() - 100
    assert store.get(sid) is None


def test_store_evicts_oldest_when_full(monkeypatch):
    """При превышении лимита самый старый сессионник вытесняется."""
    from src.alerts import sessions

    monkeypatch.setattr(sessions, "_MAX_SESSIONS", 3)
    store = PaginationStore()
    s1 = store.put([1])
    # Делаем s1 «старым»
    store._data[s1].created_at = time.time() - 1000
    s2 = store.put([2])
    s3 = store.put([3])
    s4 = store.put([4])  # должен вытеснить s1
    assert store.get(s1) is None
    assert store.get(s2) is not None
    assert store.get(s3) is not None
    assert store.get(s4) is not None


def test_page_size_constant_sane():
    assert PAGE_SIZE >= 5
    assert PAGE_SIZE <= 50
