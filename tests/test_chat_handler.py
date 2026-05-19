"""Тесты помощника help-триггеров (без сети)."""
from __future__ import annotations

from src.chat.handler import (
    _classify_funpay_system_message,
    _has_help_trigger,
    _looks_like_funpay_system_message,
    _looks_like_own_template_message,
)


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


def test_help_trigger_strips_funpay_invisible_prefix():
    triggers = {"!помощь"}
    assert _has_help_trigger("\u2064!помощь", triggers) is True


def test_own_delivery_template_is_detected_even_with_funpay_prefix():
    text = (
        "\u2064🎉 felechka1store, ваш заказ готов:\n\n"
        "• X53N8R79L2LDPYWV\n\n"
        "❓ Если что-то пошло не так — напишите !помощь"
    )
    assert _looks_like_own_template_message(text) is True


def test_funpay_paid_order_system_message_is_detected():
    text = (
        "Покупатель Booooss оплатил заказ #XDK51RB3. App Store & iTunes, "
        "Подарочные карты, АВТОВЫДАЧА. "
        "Booooss, не забудьте потом нажать кнопку «Подтвердить выполнение заказа»."
    )
    assert _looks_like_funpay_system_message(text) is True
    assert _classify_funpay_system_message(text) == "paid_order"


def test_funpay_order_confirmed_system_message_is_detected():
    text = (
        "Покупатель Macan1467 подтвердил успешное выполнение заказа "
        "#C4KPFX6M и отправил деньги продавцу lol228822."
    )
    assert _looks_like_funpay_system_message(text) is True
    assert _classify_funpay_system_message(text) == "order_confirmed"


def test_funpay_review_written_system_message_is_detected():
    text = "Покупатель felechka1store написал отзыв к заказу #F2G4TM6U."
    assert _looks_like_funpay_system_message(text) is True
    assert _classify_funpay_system_message(text) == "review_written"


def test_regular_buyer_message_is_not_system_message():
    assert _looks_like_funpay_system_message("Здравствуйте, есть товар?") is False
    assert _looks_like_funpay_system_message("!помощь") is False
    assert _classify_funpay_system_message("Здравствуйте, есть товар?") is None
