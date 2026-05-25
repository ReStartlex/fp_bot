"""
FSM-состояния shop-бота (aiogram 3).

Зачем FSM: некоторые сценарии требуют ожидать следующее сообщение от
пользователя (поиск, ввод email в checkout-форме, поддержка). Без FSM
пришлось бы держать глобальный mapping user_id → состояние, что
плохо тестируется и течёт по памяти.

aiogram даёт MemoryStorage (для разработки) или RedisStorage (если
понадобится горизонтальное масштабирование). В рамках MVP — Memory.
"""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SearchState(StatesGroup):
    """
    Юзер тапнул «🔍 Поиск» в reply-меню → бот спросил «что ищем?» →
    мы ждём следующее текстовое сообщение пользователя.

    Защита:
      - команда /cancel или кнопка «Отмена» → state.clear().
      - любой другой text входит в обработчик `_on_search_query`, который
        выводит результаты и сам очищает state.
    """
    waiting_for_query = State()
