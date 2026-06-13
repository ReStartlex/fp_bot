"""
Утилиты времени.

`datetime.utcnow()` объявлен deprecated в Python 3.12 (прод на 3.12).
Замена `datetime.now(UTC)` возвращает aware-datetime, а наша БД (SQLite)
и весь существующий код работают с NAIVE-датами в UTC — смешивать
aware/naive нельзя (TypeError при сравнении).

`utcnow()` возвращает naive-UTC (tzinfo снят) — точная семантика старого
`datetime.utcnow()`, но без deprecation. Используем везде вместо него.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Текущее время UTC как naive-datetime (совместимо с datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
