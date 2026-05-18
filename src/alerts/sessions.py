"""
In-memory хранилище для пагинации в Telegram-боте.

Зачем: callback_data в Telegram ограничен 64 байтами, поэтому нельзя
впихнуть туда полный поисковый запрос или список результатов. Вместо
этого мы храним результаты по короткому ключу (8 hex символов),
который умещается в callback_data вместе с номером страницы.

TTL — 1 час; периодическая чистка делается лениво при каждом store().
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar


T = TypeVar("T")

_DEFAULT_TTL_SECONDS = 60 * 60  # 1 час
_MAX_SESSIONS = 200  # глобальный лимит, чтобы не утечь по памяти


@dataclass
class PaginatedSession(Generic[T]):
    items: list[T]
    title: str
    created_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


class PaginationStore:
    """
    Потокобезопасный кэш для списков, которые мы листаем в боте.

    Использование:
        sid = store.put(items=services, title="Поиск 'apple'")
        sess = store.get(sid)
        for item in sess.items[page * 10 : (page + 1) * 10]: ...
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: dict[str, PaginatedSession[Any]] = {}

    def put(
        self,
        items: list[T],
        title: str = "",
        meta: dict[str, Any] | None = None,
    ) -> str:
        with self._lock:
            self._gc_locked()
            if len(self._data) >= _MAX_SESSIONS:
                # выкидываем самый старый
                oldest = min(self._data.items(), key=lambda kv: kv[1].created_at)
                self._data.pop(oldest[0], None)
            sid = secrets.token_hex(4)  # 8 hex символов
            self._data[sid] = PaginatedSession(items=list(items), title=title, meta=meta or {})
            return sid

    def get(self, sid: str) -> PaginatedSession[Any] | None:
        with self._lock:
            sess = self._data.get(sid)
            if sess is None:
                return None
            if time.time() - sess.created_at > self._ttl:
                self._data.pop(sid, None)
                return None
            return sess

    def _gc_locked(self) -> None:
        now = time.time()
        expired = [sid for sid, s in self._data.items() if now - s.created_at > self._ttl]
        for sid in expired:
            self._data.pop(sid, None)


PAGE_SIZE = 10


def paginate(items: list[T], page: int, page_size: int = PAGE_SIZE) -> tuple[list[T], int, int]:
    """
    Срез страницы.
    Возвращает (срез, нормализованный page, total_pages).
    """
    if not items:
        return [], 0, 0
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return items[start : start + page_size], page, total_pages
