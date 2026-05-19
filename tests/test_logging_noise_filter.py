"""
Логи FunPayAPI listen-loop'a про «Не удалось получить истории чатов» —
шум: они появляются из-за внутреннего рейт-лимита FunPay'a, наш poll-loop
тянет историю отдельным HTTP-клиентом и эти ошибки ни на что не влияют.

Проверяем, что наш _InterceptHandler полностью отбрасывает такие записи,
а обычные записи пропускает.
"""
from __future__ import annotations

import logging

from loguru import logger

from src.logging_setup import _InterceptHandler


def _capture(messages: list[tuple[str, str]]):
    def sink(record):
        messages.append((record.record["level"].name, record.record["message"]))
    return sink


def test_intercept_handler_drops_noisy_chat_history_message():
    received: list[tuple[str, str]] = []
    sink_id = logger.add(_capture(received), level="DEBUG", format="{message}")
    try:
        handler = _InterceptHandler()
        record = logging.LogRecord(
            name="FunPayAPI.account",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Не удалось получить истории чатов [104433092].",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    finally:
        logger.remove(sink_id)

    assert received == []


def test_intercept_handler_drops_history_retry_exhausted():
    received: list[tuple[str, str]] = []
    sink_id = logger.add(_capture(received), level="DEBUG", format="{message}")
    try:
        handler = _InterceptHandler()
        record = logging.LogRecord(
            name="FunPayAPI",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Не удалось получить истории чатов [12345]: превышено кол-во попыток.",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    finally:
        logger.remove(sink_id)

    assert received == []


def test_intercept_handler_passes_real_errors_through():
    received: list[tuple[str, str]] = []
    sink_id = logger.add(_capture(received), level="DEBUG", format="{message}")
    try:
        handler = _InterceptHandler()
        record = logging.LogRecord(
            name="my.module",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Что-то реально упало",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    finally:
        logger.remove(sink_id)

    assert any("Что-то реально упало" in msg for _, msg in received)
