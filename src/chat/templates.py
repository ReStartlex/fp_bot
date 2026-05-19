"""
Шаблоны сообщений покупателю на FunPay.

Все шаблоны — plain text (FunPay-чат не рендерит HTML).
Эмодзи используем точечно: как визуальные якоря для самых важных
строк (приветствие, готовый заказ, ошибка), не как декорацию.

Тон: спокойный, доброжелательный, по делу. Без «хей-хей» и капса.

Про команду помощи в текстах:
  FunPay иногда возвращает исходящие сообщения как входящие, поэтому
  в шаблонах не пишем триггер слитно. Покупателю показываем
  `! помощь` и явно просим убрать пробел.
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
    """Шлём СРАЗУ после того, как покупатель оплатил лот."""
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return (
            f"👋 Hi, {name}! Thanks for your purchase.\n"
            f"⏳ Preparing your order — usually 1–3 minutes.\n"
            f"Please stay in the chat; I'll send the goods here as soon as "
            f"they are ready."
        )
    return (
        f"👋 Здравствуйте, {name}! Спасибо за покупку.\n"
        f"⏳ Готовлю ваш заказ — обычно занимает 1–3 минуты.\n"
        f"Пожалуйста, оставайтесь в чате — пришлю товар сюда, как только "
        f"будет готов."
    )


def delivery(buyer: str | None, pins: list[str], lang: Lang | None = None) -> str:
    """Шлём, когда из NS пришли пины."""
    lang = lang or _lang()
    name = _addr(buyer)
    if not pins:
        if lang == "en":
            return (
                "⚠️ The supplier confirmed the order but didn't return any goods. "
                "Please write here — I'll fix it."
            )
        return (
            "⚠️ Поставщик подтвердил заказ, но товар не пришёл в ответе. "
            "Напишите сюда — я разберусь."
        )

    multiple = len(pins) > 1
    codes_block = "\n".join(f"  • {p}" for p in pins)

    if lang == "en":
        intro = (
            f"🎉 {name}, your order is ready! {len(pins)} items:"
            if multiple
            else f"🎉 {name}, your order is ready!"
        )
        return (
            f"{intro}\n\n"
            f"{codes_block}\n\n"
            f"📌 Please activate within 24 hours.\n"
            f"❓ If something goes wrong, write here — type ! help "
            f"(without the space) and I'll "
            f"jump in personally.\n"
            f"⭐ If everything is fine, a short feedback would mean a lot."
        )

    intro = (
        f"🎉 {name}, ваш заказ готов — {len(pins)} шт:"
        if multiple
        else f"🎉 {name}, ваш заказ готов:"
    )
    return (
        f"{intro}\n\n"
        f"{codes_block}\n\n"
        f"📌 Пожалуйста, активируйте в течение 24 часов.\n"
        f"❓ Если что-то пошло не так — напишите ! помощь "
        f"(слитно, без пробела), и я подключусь лично.\n"
        f"⭐ Если всё хорошо, буду благодарен за отзыв."
    )


def delivery_delayed(buyer: str | None, lang: Lang | None = None) -> str:
    """Шлём, если NS задерживает выдачу."""
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return (
            f"⏳ {name}, the order is taking longer than usual.\n"
            f"I'm watching it and will deliver as soon as the supplier "
            f"responds. If nothing arrives within 15 minutes, I'll start a "
            f"refund automatically."
        )
    return (
        f"⏳ {name}, заказ занимает чуть больше обычного.\n"
        f"Я слежу за ним и выдам сразу, как поставщик ответит. Если за "
        f"15 минут ничего не придёт — оформлю возврат автоматически."
    )


def delivery_failed(buyer: str | None, lang: Lang | None = None) -> str:
    """Шлём, если NS отказал в заказе."""
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return (
            f"😔 Sorry, {name}, the supplier rejected this order.\n"
            f"💸 I've initiated a refund — please confirm it on the FunPay "
            f"order page.\n"
            f"My apologies for the inconvenience."
        )
    return (
        f"😔 К сожалению, {name}, поставщик отклонил этот заказ.\n"
        f"💸 Оформляю возврат — подтвердите его на странице заказа на FunPay.\n"
        f"Прошу прощения за неудобство."
    )


def post_review(buyer: str | None, lang: Lang | None = None) -> str:
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return f"🙌 Thanks for the feedback, {name}! Always happy to help."
    return f"🙌 Спасибо за отзыв, {name}! Всегда рад помочь."


def order_confirmed_review_request(
    buyer: str | None, lang: Lang | None = None
) -> str:
    """Шлём после системного сообщения FunPay о подтверждении заказа."""
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        return (
            f"💚 {name}, thanks for confirming the order!\n"
            f"✨ I'm glad everything went smoothly.\n"
            f"⭐ If you liked the service, a short review on FunPay would "
            f"help me a lot. Have a great day!"
        )
    return (
        f"💚 {name}, спасибо за подтверждение заказа!\n"
        f"✨ Очень рад, что всё прошло успешно.\n"
        f"⭐ Если сервис понравился, буду очень благодарен за короткий "
        f"отзыв на FunPay. Хорошего дня!"
    )


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
    Тон: коротко, по делу. Чётко проговариваем, как вызвать оператора.
    """
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        if working_now:
            return (
                f"👋 Hi, {name}!\n"
                f"⚡ Delivery is automatic: the goods arrive in this chat "
                f"within seconds after payment.\n"
                f"❓ If you have any questions, just write here — or type "
                f"! help without the space to ping me personally."
            )
        return (
            f"👋 Hi, {name}!\n"
            f"🕒 Working hours: {wh.format_window()} ({wh.tz_name}). "
            f"Auto-delivery still works around the clock — the goods arrive "
            f"in chat within seconds after payment.\n"
            f"❓ If you need a human, type ! help without the space — "
            f"I'll reply first thing in the morning."
        )

    if working_now:
        return (
            f"👋 Здравствуйте, {name}!\n"
            f"⚡ Выдача товара автоматическая — приходит сюда, в чат, в "
            f"течение нескольких секунд после оплаты.\n"
            f"❓ Если есть вопросы — просто напишите сюда. Чтобы позвать "
            f"меня лично — отправьте ! помощь (слитно, без пробела)."
        )
    return (
        f"👋 Здравствуйте, {name}!\n"
        f"🕒 Я работаю с {wh.format_window()} ({wh.tz_name}).\n"
        f"⚡ Автовыдача работает круглосуточно — товар придёт в чат за "
        f"несколько секунд после оплаты.\n"
        f"❓ Если нужен живой человек — отправьте ! помощь "
        f"(слитно, без пробела), я отвечу утром."
    )


# -------------------- Реакция на !help --------------------

def help_acknowledged(
    buyer: str | None,
    *,
    working_now: bool,
    wh: WorkingHours,
    lang: Lang | None = None,
) -> str:
    """Шлём, когда покупатель отправил один из help-триггеров."""
    lang = lang or _lang()
    name = _addr(buyer)
    if lang == "en":
        if working_now:
            return (
                f"✅ Got it, {name}.\n"
                f"📲 I've notified the seller — they'll join the chat shortly."
            )
        return (
            f"✅ Got it, {name}.\n"
            f"🌙 It's outside working hours right now ({wh.format_window()} "
            f"{wh.tz_name}). The seller has been notified and will reply in "
            f"the morning."
        )

    if working_now:
        return (
            f"✅ Принято, {name}!\n"
            f"📲 Уведомил продавца — он подключится к чату в ближайшее время."
        )
    return (
        f"✅ Принято, {name}!\n"
        f"🌙 Сейчас вне рабочих часов ({wh.format_window()} {wh.tz_name}). "
        f"Продавец получил уведомление и ответит вам утром."
    )
