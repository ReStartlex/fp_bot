"""
Простой in-memory rate-limit для публичных auth-ручек сайта (P2-6).

Защита от брутфорса логина (Telegram/OAuth/webapp). Скользящее окно на
ключ `path:ip`. Лимитер живёт на `app.state` (свой на каждое приложение),
поэтому тесты не пересекаются. За reverse-proxy IP берём из
X-Forwarded-For (nginx доверенный), иначе request.client.

Это НЕ распределённый лимитер (один процесс) — для нашего single-VPS
деплоя достаточно. Webhook CryptoBot подписан, Telegram login — HMAC;
здесь добавляем только частотный барьер.
"""
from __future__ import annotations

from collections import defaultdict, deque
from time import monotonic

from fastapi import HTTPException, Request, status


class InMemoryRateLimiter:
    __slots__ = ("_hits",)

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        """True если запрос в пределах лимита (и регистрирует его)."""
        now = monotonic()
        dq = self._hits[key]
        cutoff = now - window_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def auth_rate_limit(request: Request) -> None:
    """FastAPI-dependency: 429, если с одного IP слишком частые auth-запросы."""
    from src.config import get_settings

    limiter: InMemoryRateLimiter | None = getattr(
        request.app.state, "rate_limiter", None
    )
    if limiter is None:
        return
    settings = get_settings()
    limit = int(getattr(settings, "site_auth_rate_limit", 10))
    window = float(getattr(settings, "site_auth_rate_window_seconds", 60))
    if limit <= 0:
        return  # 0 = выключено
    key = f"{request.url.path}:{client_ip(request)}"
    if not limiter.allow(key, limit, window):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many requests — slow down",
        )
