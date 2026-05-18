"""Работа с рабочими часами и часовыми поясами."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


@dataclass
class WorkingHours:
    """Окно рабочих часов в указанной таймзоне (start ≤ end, в часах)."""
    start_hour: int
    end_hour: int
    tz_name: str = "Europe/Moscow"

    def _tz(self):
        if ZoneInfo is None:
            return None
        try:
            return ZoneInfo(self.tz_name)
        except Exception:
            return None

    def now_local(self, now: Optional[datetime] = None) -> datetime:
        tz = self._tz()
        base = now or datetime.utcnow().replace(microsecond=0)
        if tz is None:
            return base
        if base.tzinfo is None:
            return datetime.now(tz)
        return base.astimezone(tz)

    def is_working_now(self, now: Optional[datetime] = None) -> bool:
        local = self.now_local(now)
        return self._is_working(local.time())

    def _is_working(self, t: time) -> bool:
        if self.start_hour <= self.end_hour:
            return self.start_hour <= t.hour < self.end_hour
        # окно через полночь (например 22..6)
        return t.hour >= self.start_hour or t.hour < self.end_hour

    def next_working_time(self, now: Optional[datetime] = None) -> datetime:
        """Ближайший момент, когда мы снова в рабочих часах."""
        local = self.now_local(now)
        if self._is_working(local.time()):
            return local
        target_today = local.replace(
            hour=self.start_hour, minute=0, second=0, microsecond=0
        )
        if local < target_today:
            return target_today
        return target_today + timedelta(days=1)

    def format_window(self) -> str:
        return f"{self.start_hour:02d}:00–{self.end_hour:02d}:00"
