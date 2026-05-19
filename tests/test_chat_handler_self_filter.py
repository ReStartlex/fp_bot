"""
ChatHandler не должен реагировать на сообщения, где author_username = my_username.

Подстраховка от случая, когда watcher всё-таки пропустил собственное
сообщение (например, при изменении HTML-структуры FunPay).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.chat.handler import ChatHandler
from src.config import Settings
from src.funpay.events import FunPayMessageEvent


def _make_handler(my_username: str):
    fp = MagicMock()
    fp.my_username = my_username
    fp.account = SimpleNamespace(id=1, username=my_username)
    fp.send_message = AsyncMock()
    tg = MagicMock()
    tg.send = AsyncMock()
    settings = Settings(  # type: ignore[call-arg]
        ns_user_id=1, ns_login="x", ns_password="x",
        ns_api_secret="QQ==", funpay_golden_key="x", funpay_user_id=1,
    )
    return ChatHandler(fp, telegram=tg, settings=settings), fp, tg


def test_handler_ignores_message_from_self_by_username():
    handler, fp, tg = _make_handler(my_username="lol228822")
    event = FunPayMessageEvent(
        chat_id=104433092,
        chat_username="VOLT228822",
        author_id=None,
        author_username="lol228822",
        text="Здравствуйте! ... напишите !помощь — и я подключусь.",
        is_my_message=False,
    )
    asyncio.run(handler.on_message(event))
    # ни одного исходящего сообщения, ни одного телеграм-уведомления
    fp.send_message.assert_not_called()
    tg.send.assert_not_called()


def test_handler_ignores_when_is_my_message_true():
    handler, fp, tg = _make_handler(my_username="lol228822")
    event = FunPayMessageEvent(
        chat_id=104433092,
        chat_username="VOLT228822",
        author_id=1,
        author_username="lol228822",
        text="что угодно",
        is_my_message=True,
    )
    asyncio.run(handler.on_message(event))
    fp.send_message.assert_not_called()
    tg.send.assert_not_called()


def test_handler_ignores_when_only_message_id_matches():
    """is_my_message=True достаточно — username сверять не обязательно."""
    handler, fp, tg = _make_handler(my_username="lol228822")
    event = FunPayMessageEvent(
        chat_id=1,
        chat_username="VOLT228822",
        author_id=1,
        author_username=None,
        text="хоть что-то",
        is_my_message=True,
    )
    asyncio.run(handler.on_message(event))
    fp.send_message.assert_not_called()
    tg.send.assert_not_called()


def test_handler_ignores_own_delivery_echo_with_wrong_author():
    handler, fp, tg = _make_handler(my_username="lol228822")
    event = FunPayMessageEvent(
        chat_id=104433092,
        chat_username="felechka1store",
        author_id=None,
        author_username="felechka1store",
        text=(
            "\u2064🎉 felechka1store, ваш заказ готов:\n\n"
            "• X53N8R79L2LDPYWV\n\n"
            "❓ Если что-то пошло не так — напишите !помощь, "
            "и я подключусь лично."
        ),
        is_my_message=False,
    )
    asyncio.run(handler.on_message(event))
    fp.send_message.assert_not_called()
    tg.send.assert_not_called()


def test_handler_ignores_funpay_paid_order_system_message():
    handler, fp, tg = _make_handler(my_username="lol228822")
    event = FunPayMessageEvent(
        chat_id=104433092,
        chat_username="Booooss",
        author_id=None,
        author_username="Booooss",
        text=(
            "Покупатель Booooss оплатил заказ #XDK51RB3. "
            "App Store & iTunes, Подарочные карты, АВТОВЫДАЧА. "
            "Booooss, не забудьте потом нажать кнопку "
            "«Подтвердить выполнение заказа»."
        ),
        is_my_message=False,
    )
    asyncio.run(handler.on_message(event))
    fp.send_message.assert_not_called()
    tg.send.assert_not_called()
