"""Тесты помощника help-триггеров (без сети)."""
from __future__ import annotations

from src.chat.handler import (
    _classify_funpay_system_message,
    _extract_funpay_order_id,
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


def test_funpay_admin_confirmed_system_message_is_detected():
    """Главный кейс: ~50% покупателей не подтверждают сами, и саппорт
    FunPay делает это вручную через 24ч. Раньше бот отвечал
    приветствием на это сообщение — некорректно."""
    text = (
        "Администратор Palmira подтвердил успешное выполнение заказа "
        "#UGW9A7CQ и отправил деньги продавцу lol228822."
    )
    assert _looks_like_funpay_system_message(text) is True
    assert _classify_funpay_system_message(text) == "order_confirmed_by_admin"


def test_funpay_admin_confirmed_english_locale():
    """FunPay поддерживает английский; имя админа может быть разное."""
    text = (
        "Administrator JohnDoe confirmed successful order completion #ABC123XYZ"
    )
    assert _classify_funpay_system_message(text) == "order_confirmed_by_admin"


def test_admin_confirmed_takes_priority_over_buyer_confirmed():
    """Анти-регрессия: ru_order_confirmed (buyer) тоже содержит
    «подтвердил успешное выполнение заказа». Важно, чтобы ветка с
    «администратор» проверялась РАНЬШЕ — иначе админ-confirm
    будет ошибочно классифицирован как buyer-confirm."""
    text = (
        "Администратор Palmira подтвердил успешное выполнение заказа "
        "#UGW9A7CQ и отправил деньги продавцу lol228822."
    )
    kind = _classify_funpay_system_message(text)
    assert kind == "order_confirmed_by_admin", (
        f"Ожидался admin-confirm, но получили {kind!r}. Возможно ветка "
        f"buyer-confirm срабатывает раньше — это БАГ."
    )


def test_extract_funpay_order_id_from_admin_confirm():
    text = (
        "Администратор Palmira подтвердил успешное выполнение заказа "
        "#UGW9A7CQ и отправил деньги продавцу lol228822."
    )
    assert _extract_funpay_order_id(text) == "UGW9A7CQ"


def test_extract_funpay_order_id_from_buyer_confirm():
    text = (
        "Покупатель Macan1467 подтвердил успешное выполнение заказа "
        "#C4KPFX6M и отправил деньги продавцу lol228822."
    )
    assert _extract_funpay_order_id(text) == "C4KPFX6M"


def test_extract_funpay_order_id_returns_none_when_no_id():
    """Текст без #XXX — None."""
    assert _extract_funpay_order_id("Здравствуйте, есть товар?") is None
    assert _extract_funpay_order_id("") is None
    assert _extract_funpay_order_id(None) is None  # type: ignore[arg-type]


def test_extract_funpay_order_id_ignores_lowercase_or_short_ids():
    """FunPay order_id всегда 6-12 заглавных букв/цифр; защищаемся
    от false-positive на случайных «#test» в тексте покупателя."""
    assert _extract_funpay_order_id("привет #test") is None
    assert _extract_funpay_order_id("hello #ab12") is None  # короче 6
    assert _extract_funpay_order_id("see order #ABC") is None  # короче 6


def test_extract_funpay_order_id_picks_first_when_multiple():
    """Если в тексте несколько #ID (теоретически) — берём первый."""
    assert _extract_funpay_order_id("orders #ABC12345 and #XYZ98765") == "ABC12345"


def test_funpay_review_written_system_message_is_detected():
    text = "Покупатель felechka1store написал отзыв к заказу #F2G4TM6U."
    assert _looks_like_funpay_system_message(text) is True
    assert _classify_funpay_system_message(text) == "review_written"


def test_regular_buyer_message_is_not_system_message():
    assert _looks_like_funpay_system_message("Здравствуйте, есть товар?") is False
    assert _looks_like_funpay_system_message("!помощь") is False
    assert _classify_funpay_system_message("Здравствуйте, есть товар?") is None
