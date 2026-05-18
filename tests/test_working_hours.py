"""Тесты рабочих часов и нормального дня/ночи."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.chat.schedule import WorkingHours


def _moscow(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Moscow"))


def test_within_window():
    wh = WorkingHours(start_hour=12, end_hour=23, tz_name="Europe/Moscow")
    assert wh.is_working_now(_moscow(2026, 5, 18, 15, 30)) is True


def test_at_boundary_start():
    wh = WorkingHours(start_hour=12, end_hour=23, tz_name="Europe/Moscow")
    # 12:00 включительно
    assert wh.is_working_now(_moscow(2026, 5, 18, 12, 0)) is True


def test_at_boundary_end():
    wh = WorkingHours(start_hour=12, end_hour=23, tz_name="Europe/Moscow")
    # 23:00 исключительно
    assert wh.is_working_now(_moscow(2026, 5, 18, 23, 0)) is False
    assert wh.is_working_now(_moscow(2026, 5, 18, 22, 59)) is True


def test_outside_morning():
    wh = WorkingHours(start_hour=12, end_hour=23, tz_name="Europe/Moscow")
    assert wh.is_working_now(_moscow(2026, 5, 18, 9, 0)) is False


def test_outside_night():
    wh = WorkingHours(start_hour=12, end_hour=23, tz_name="Europe/Moscow")
    assert wh.is_working_now(_moscow(2026, 5, 18, 3, 0)) is False


def test_window_across_midnight():
    # окно с 22:00 до 06:00 (например — ночная смена)
    wh = WorkingHours(start_hour=22, end_hour=6, tz_name="Europe/Moscow")
    assert wh.is_working_now(_moscow(2026, 5, 18, 23, 0)) is True
    assert wh.is_working_now(_moscow(2026, 5, 18, 1, 0)) is True
    assert wh.is_working_now(_moscow(2026, 5, 18, 7, 0)) is False
    assert wh.is_working_now(_moscow(2026, 5, 18, 20, 0)) is False


def test_format_window():
    wh = WorkingHours(start_hour=12, end_hour=23, tz_name="Europe/Moscow")
    assert wh.format_window() == "12:00–23:00"


def test_next_working_time_morning_before_window():
    wh = WorkingHours(start_hour=12, end_hour=23, tz_name="Europe/Moscow")
    now = _moscow(2026, 5, 18, 9, 30)
    nxt = wh.next_working_time(now)
    assert nxt.hour == 12 and nxt.minute == 0 and nxt.day == 18


def test_next_working_time_after_window_rolls_to_next_day():
    wh = WorkingHours(start_hour=12, end_hour=23, tz_name="Europe/Moscow")
    now = _moscow(2026, 5, 18, 23, 30)
    nxt = wh.next_working_time(now)
    assert nxt.hour == 12 and nxt.day == 19
