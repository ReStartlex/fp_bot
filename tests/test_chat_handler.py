"""Тесты помощника help-триггеров (без сети)."""
from __future__ import annotations

from src.chat.handler import _has_help_trigger


def test_help_trigger_simple():
    triggers = {"!help", "!помощь", "!sos"}
    assert _has_help_trigger("Что-то не работает !помощь", triggers) is True


def test_help_trigger_case_insensitive():
    triggers = {"!help", "!помощь"}
    assert _has_help_trigger("Прошу !ПОМОЩЬ", triggers) is True


def test_help_trigger_no_match():
    triggers = {"!help", "!помощь"}
    assert _has_help_trigger("Привет, когда придёт код?", triggers) is False


def test_help_trigger_empty_text():
    assert _has_help_trigger("", {"!help"}) is False


def test_help_trigger_empty_triggers():
    assert _has_help_trigger("!help", set()) is False


def test_help_trigger_substring_safe():
    # триггер — !help, в тексте без восклицательного знака не должно срабатывать
    triggers = {"!help"}
    assert _has_help_trigger("please help me", triggers) is False
