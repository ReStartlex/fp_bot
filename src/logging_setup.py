"""Настройка loguru: консоль + файл с ротацией."""
from __future__ import annotations

import sys

from loguru import logger

from src.config import Settings, get_settings


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
