"""Тесты шаблонов сообщений FunPay."""
from __future__ import annotations

from src.chat import templates


def test_order_received_ru():
    text = templates.order_received("Vasya", lang="ru")
    assert "Vasya" in text
    assert "покупку" in text.lower()


def test_order_received_en():
    text = templates.order_received("Vasya", lang="en")
    assert "Vasya" in text
    assert "thanks" in text.lower()


def test_delivery_with_codes():
    text = templates.delivery("Vasya", ["CODE-AAA", "CODE-BBB"], lang="ru")
    assert "CODE-AAA" in text
    assert "CODE-BBB" in text


def test_delivery_empty():
    text = templates.delivery("Vasya", [], lang="ru")
    # graceful fallback
    assert "не пришли" in text or "поставщик" in text


def test_delivery_failed_ru():
    text = templates.delivery_failed("Vasya", lang="ru")
    assert "возврат" in text.lower()
