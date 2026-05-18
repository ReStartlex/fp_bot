"""
Шаблоны сообщений покупателю на FunPay.

Все шаблоны — plain text (FunPay-чат не рендерит HTML).
Эмодзи используем умеренно: они работают как визуальный якорь, а не как
украшение каждой строки.

Тон: спокойный, доброжелательный, по делу. Без "хей-хей" и капса.
"""
from __future__ import annotations

from typing import Literal

from src.chat.schedule import WorkingHours
from src.config import get_settings


Lang = Literal["ru", "en"]


def _lang() -> Lang:
    return get_settings().funpay_chat_language  # type: ignore[return-value]


def _addr(buyer: str | None) -> str:
    return buyer if buyer and buyer.strip() else "друг"


# -------------------- При покупке --------------------

def order_received(buyer: str | None, lang: Lang | None = None) -> str:
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return (
            f"Hi, {name}! 👋 Thanks for your purchase.\n"
            f"Preparing your order, it usually takes 1–3 minutes."
        )
    return (
        f"Здравствуйте, {name}! 👋\n"
        f"Спасибо за покупку, готовлю ваш заказ. Обычно занимает 1–3 минуты."
    )


def delivery(buyer: str | None, pins: list[str], lang: Lang | None = None) -> str:
    lang = lang or _lang()
    name = _addr(buyer)
    if not pins:
        return (
            "Заказ выполнен на стороне поставщика, но коды не пришли в ответе. "
            "Свяжитесь со мной, я разберусь."
        )

    codes_block = "\n".join(f"  {p}" for p in pins)
    if lang == "en":
        return (
            f"{name}, here is your order 🎉\n\n"
            f"{codes_block}\n\n"
            f"Please activate the code within 24 hours.\n"
            f"If anything goes wrong — write me here, I will help.\n"
            f"If everything is fine, a short feedback would be appreciated ⭐"
        )
    return (
        f"{name}, ваш заказ готов 🎉\n\n"
        f"{codes_block}\n\n"
        f"Пожалуйста, активируйте код в течение 24 часов.\n"
        f"Если что-то пошло не так — напишите сюда, я помогу.\n"
        f"Если всё хорошо, буду благодарен за отзыв ⭐"
    )


def delivery_delayed(buyer: str | None, lang: Lang | None = None) -> str:
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return (
            f"{name}, the order is taking longer than usual. "
            f"I'm watching it — will deliver as soon as the supplier responds. "
            f"If it doesn't arrive within 15 minutes, I'll start a refund."
        )
    return (
        f"{name}, заказ занимает чуть больше обычного. "
        f"Я слежу за ним и выдам сразу, как поставщик ответит. "
        f"Если не получится в течение 15 минут — оформлю возврат."
    )


def delivery_failed(buyer: str | None, lang: Lang | None = None) -> str:
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return (
            f"Sorry, {name}, the supplier rejected this order. "
            f"I've initiated a refund — please confirm it on FunPay's order page. "
            f"My apologies for the inconvenience."
        )
    return (
        f"К сожалению, поставщик отклонил этот заказ, {name}. "
        f"Оформляю возврат — подтвердите его на странице заказа на FunPay. "
        f"Прошу прощения за неудобство."
    )


def post_review(buyer: str | None, lang: Lang | None = None) -> str:
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return f"Thanks for the feedback, {name}! Always happy to help."
    return f"Спасибо за отзыв, {name}! Всегда рад помочь."


# -------------------- В чате до покупки --------------------

def greeting_pre_purchase(
    buyer: str | None,
    *,
    working_now: bool,
    wh: WorkingHours,
    lang: Lang | None = None,
) -> str:
    """
    Приветствие, когда человек написал, но ещё ничего не купил.
    Тон: коротко, по делу, объясняем как работает выдача.
    """
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        if working_now:
            return (
                f"Hi, {name}! 👋\n"
                f"Delivery is automatic: codes arrive in chat within seconds after payment.\n"
                f"If you have any questions, write me here or type <code>!help</code> to "
                f"ping the seller."
            )
        return (
            f"Hi, {name}! 👋\n"
            f"Working hours are {wh.format_window()} ({wh.tz_name}). "
            f"Auto-delivery still works at any time — codes will arrive in chat within "
            f"seconds after payment.\n"
            f"If you need a human, type <code>!help</code>."
        )

    if working_now:
        return (
            f"Здравствуйте, {name}! 👋\n"
            f"Выдача автоматическая — коды приходят в чат в течение нескольких секунд "
            f"после оплаты.\n"
            f"Если есть вопросы, пишите сюда или напишите !помощь — и я подключусь."
        )
    return (
        f"Здравствуйте, {name}! 👋\n"
        f"Я работаю с {wh.format_window()} ({wh.tz_name}). "
        f"Выдача товара автоматическая и работает круглосуточно — коды приходят в "
        f"чат в течение нескольких секунд после оплаты.\n"
        f"Если нужен человек, напишите !помощь — я свяжусь с вами утром."
    )


# -------------------- Реакция на !help --------------------

def help_acknowledged(
    buyer: str | None,
    *,
    working_now: bool,
    wh: WorkingHours,
    lang: Lang | None = None,
) -> str:
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        if working_now:
            return (
                f"Got it, {name} ✅\n"
                f"I've notified the seller — they will join the chat shortly."
            )
        return (
            f"Got it, {name} ✅\n"
            f"It's outside working hours right now ({wh.format_window()} {wh.tz_name}). "
            f"The seller has been notified and will reply in the morning."
        )

    if working_now:
        return (
            f"Принято, {name} ✅\n"
            f"Уведомил продавца — он подключится к чату в ближайшее время."
        )
    return (
        f"Принято, {name} ✅\n"
        f"Сейчас вне рабочих часов ({wh.format_window()} {wh.tz_name}). "
        f"Продавец получил уведомление и ответит вам утром."
    )
