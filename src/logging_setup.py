"""Настройка loguru: консоль + файл с ротацией."""
from __future__ import annotations

import logging
import sys

from loguru import logger

from src.config import Settings, get_settings


# Подстроки, которые мы знаем как шум от FunPayAPI listen-loop'a
# (рейт-лимит на запрос историй чатов). Наш собственный poll-loop
# забирает истории отдельным HTTP-клиентом, поэтому эти строки бесполезны.
_NOISY_PATTERNS = (
    "не удалось получить истории чатов",
    "не удалось получить историю чата",
)


class _InterceptHandler(logging.Handler):
    """
    Перенаправляет логи stdlib logging в loguru.
    Нужно, чтобы сообщения FunPayAPI.Runner и других библиотек
    (которые юзают logging.getLogger), оказывались в наших логах,
    а не сыпались отдельной строкой через print() / stderr.

    Дополнительно: понижает уровень/отбрасывает шумные сообщения
    FunPayAPI о повторных попытках получить историю чата.
    """

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        lowered = message.lower()
        if any(pat in lowered for pat in _NOISY_PATTERNS):
            # Полностью игнорируем — это спам, который ни на что не влияет.
            return
        try:
            level = logger.level(record.levelname).name
        except (AttributeError, ValueError):
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, message)


def setup_logging(settings: Settings | None = None) -> None:
    """Сконфигурировать loguru. Безопасно вызывать многократно."""
    settings = settings or get_settings()
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
            "<level>{level: <7}</level> "
            "<cyan>{name}</cyan> | {message}"
        ),
        colorize=True,
    )
    logger.add(
        settings.log_path / "bridge_{time:YYYY-MM-DD}.log",
        level=settings.log_level,
        rotation="00:00",
        retention="14 days",
        compression="zip",
        encoding="utf-8",
    )

    # Подменяем root logger stdlib logging нашим хэндлером, чтобы
    # любая библиотека, пишущая через logging.* (например, FunPayAPI,
    # aiogram, apscheduler) ушла в общий loguru-форматтер.
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    # Шумные внешние логгеры — режем до WARNING, чтобы не засирали лог.
    for noisy in (
        "FunPayAPI.account",
        "FunPayAPI.updater",
        "FunPayAPI.updater.runner",
        "apscheduler.executors.default",
        "apscheduler.scheduler",
        "aiogram.event",
        "aiogram.dispatcher",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
