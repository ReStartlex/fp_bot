"""Нормализованные события FunPay (минимум, что нужно нашей логике)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FunPayMessageEvent:
    """Любое сообщение в чате с покупателем (включая наши собственные)."""
    chat_id: int
    chat_username: Optional[str]
    author_id: Optional[int]
    author_username: Optional[str]
    text: str
    is_my_message: bool
