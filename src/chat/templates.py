"""
Шаблоны сообщений покупателю на FunPay.

Дизайн:
- Шаблон = функция, принимающая контекст (kwargs).
- Возвращает готовый текст для FunPay-чата (max ~1000 символов, без HTML — FunPay-чат plain text).
- Поддержка двух языков (ru/en) — выбирается по настройке `funpay_chat_language`.

Все шаблоны намеренно простые, дружелюбные, без эмодзи-спама.
"""
from __future__ import annotations

from typing import Literal

from src.config import get_settings


Lang = Literal["ru", "en"]


def _lang() -> Lang:
    return get_settings().funpay_chat_language  # type: ignore[return-value]


def order_received(buyer: str, lang: Lang | None = None) -> str:
    """Сразу после получения уведомления о покупке."""
    lang = lang or _lang()
    if lang == "en":
        return (
            f"Hello, {buyer}! Thanks for your purchase.\n"
            f"I'm preparing your order, it will arrive in 1–3 minutes."
        )
    return (
        f"Здравствуйте, {buyer}! Спасибо за покупку.\n"
        f"Готовлю ваш заказ, выдача займёт 1–3 минуты."
    )


def delivery(buyer: str, pins: list[str], lang: Lang | None = None) -> str:
    """Доставка кодов/пинов."""
    lang = lang or _lang()
    if not pins:
        return (
            "Готово! Заказ выполнен на стороне поставщика, "
            "но коды не пришли в ответе. Свяжитесь со мной."
        )
    codes_block = "\n".join(f"`{p}`" for p in pins)
    if lang == "en":
        return (
            f"Here's your order, {buyer}:\n\n"
            f"{codes_block}\n\n"
            f"Please activate the code(s) within 24 hours. "
            f"If something goes wrong — write me here, I'll help.\n"
            f"If everything is good, please leave a feedback ⭐"
        )
    return (
        f"Ваш заказ, {buyer}:\n\n"
        f"{codes_block}\n\n"
        f"Активируйте код в течение 24 часов. "
        f"Если что-то пошло не так — пишите сюда, помогу.\n"
        f"Если всё хорошо — буду благодарен за отзыв ⭐"
    )


def delivery_delayed(buyer: str, lang: Lang | None = None) -> str:
    """Заказ обрабатывается долго."""
    lang = lang or _lang()
    if lang == "en":
        return (
            f"{buyer}, the order is taking longer than usual. "
            f"I'm watching it — will deliver as soon as the supplier responds. "
            f"If it doesn't arrive within 15 minutes, I'll issue a refund."
        )
    return (
        f"{buyer}, заказ занимает чуть больше времени обычного. "
        f"Я слежу за ним и выдам сразу как поставщик ответит. "
        f"Если не получится в течение 15 минут — оформлю возврат."
    )


def delivery_failed(buyer: str, lang: Lang | None = None) -> str:
    """Не получилось выдать (refund, timeout)."""
    lang = lang or _lang()
    if lang == "en":
        return (
            f"Sorry, {buyer}, the supplier rejected this order. "
            f"I've initiated a refund — please confirm it on FunPay's order page."
        )
    return (
        f"К сожалению, поставщик отклонил этот заказ. "
        f"Оформляю возврат — подтвердите его на странице заказа на FunPay. "
        f"Извините за неудобство."
    )


def post_review(buyer: str, lang: Lang | None = None) -> str:
    """Просьба отзыва после отзыва покупателя (опционально)."""
    lang = lang or _lang()
    if lang == "en":
        return f"Thank you for the feedback, {buyer}! Always happy to help."
    return f"Спасибо за отзыв, {buyer}! Всегда рад помочь."
