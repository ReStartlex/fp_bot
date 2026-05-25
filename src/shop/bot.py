"""
Telegram-бот для покупателей (отдельный от owner-бота из src/alerts/bot.py).

Архитектурно идентичен `TelegramBot` из alerts/bot.py:
    - aiogram 3.x, long-polling
    - lifecycle start/stop
    - все handler'ы как методы класса
Но это — public-bot, авторизация по telegram_user_id (любой человек).

В этом файле — только skeleton Спринта 1:
    /start [ref_N]  — регистрация + парсинг реф-ссылки
    /help           — короткая справка
    /balance        — показать внутренний баланс
    /ref            — выдать персональную реф-ссылку
Каталог, оплата, заказы — будут в следующих спринтах.
"""
from __future__ import annotations

import asyncio
import html
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from src.config import Settings, get_settings
from src.db.session import session_factory
from src.shop.repo import (
    apply_balance_change,
    attach_referral,
    get_catalog_service,
    get_or_create_user,
    list_categories_for_ui,
    list_services_in_category,
    parse_referral_payload,
)


CATALOG_PAGE_SIZE = 8


# Алерт-callback в owner-бота при первом старте бота, новой регистрации и т.п.
# (опциональный — если None, просто логируем).
OwnerNotify = Callable[[str], Awaitable[None]]


HELP_TEXT = (
    "🛒 <b>Магазин подарочных карт</b>\n\n"
    "<b>Доступные команды</b>\n"
    "/start — начать пользоваться ботом\n"
    "/catalog — каталог товаров (скоро)\n"
    "/balance — мой внутренний баланс\n"
    "/ref — моя реферальная ссылка (1% кэшбэка с покупок друзей)\n"
    "/orders — мои заказы (скоро)\n"
    "/support — связаться с оператором (скоро)\n"
    "/help — это сообщение\n\n"
    "💡 <i>Магазин в режиме раннего доступа: оплата и каталог "
    "включатся в ближайшие дни.</i>"
)


def format_rub(kopecks: int) -> str:
    """1234500 копеек → '12 345 ₽' для отображения."""
    rub_int = kopecks // 100
    rub_frac = kopecks % 100
    # Группируем тысячи неразрывным пробелом для красивого отображения.
    groups = []
    s = str(rub_int)
    while s:
        groups.append(s[-3:])
        s = s[:-3]
    integer_part = "\u00a0".join(reversed(groups))
    if rub_frac:
        return f"{integer_part},{rub_frac:02d}\u00a0₽"
    return f"{integer_part}\u00a0₽"


class ShopBot:
    """
    Покупательский Telegram-бот для shop-витрины.

    Контракт жизненного цикла:
        bot = ShopBot(settings)
        await bot.start()      # neблокирует, запускает polling в task
        ...
        await bot.stop()       # graceful shutdown

    Не запускается если shop_enabled=False или shop_telegram_bot_token не задан —
    silently no-op (это позволяет деплоить код в прод без shop-токена и
    включать shop тумблером в .env).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        owner_notify: OwnerNotify | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owner_notify = owner_notify
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._task: asyncio.Task | None = None
        self._username: str | None = None

    @property
    def enabled(self) -> bool:
        s = self._settings
        return s.shop_enabled and s.shop_telegram_bot_token is not None

    @property
    def username(self) -> str | None:
        """@username бота для построения реф-ссылок. None пока не стартовал."""
        return self._username

    # ─────────────── lifecycle ───────────────

    async def start(self) -> None:
        if not self.enabled:
            logger.info(
                "Shop-бот отключён (shop_enabled=false или shop_telegram_bot_token не задан)"
            )
            return

        token = self._settings.shop_telegram_bot_token.get_secret_value()  # type: ignore[union-attr]
        self._bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()
        self._register_handlers()

        try:
            await self._bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            logger.debug(f"shop delete_webhook: {exc}")
        try:
            await self._set_bot_commands()
        except Exception as exc:
            logger.debug(f"shop set_my_commands: {exc}")

        me = await self._bot.get_me()
        self._username = me.username
        logger.info(f"Shop-бот @{me.username} стартовал (long-polling)")
        if self._owner_notify is not None:
            try:
                await self._owner_notify(
                    f"🛒 Shop-бот <b>@{html.escape(me.username or '?')}</b> запущен"
                )
            except Exception as exc:
                logger.debug(f"shop owner_notify on start: {exc}")
        self._task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="shop-bot-polling",
        )

    async def stop(self) -> None:
        if self._dp is not None:
            try:
                await self._dp.stop_polling()
            except Exception as exc:
                logger.debug(f"shop stop_polling: {exc}")
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._task = None
        if self._bot is not None:
            try:
                await self._bot.session.close()
            except Exception as exc:
                logger.debug(f"shop bot session close: {exc}")
            self._bot = None
        self._dp = None
        logger.info("Shop-бот остановлен")

    async def _set_bot_commands(self) -> None:
        if self._bot is None:
            return
        await self._bot.set_my_commands([
            BotCommand(command="start", description="Начать"),
            BotCommand(command="catalog", description="Каталог товаров"),
            BotCommand(command="balance", description="Мой баланс"),
            BotCommand(command="ref", description="Реф-ссылка"),
            BotCommand(command="orders", description="Мои заказы"),
            BotCommand(command="support", description="Поддержка"),
            BotCommand(command="help", description="Справка"),
        ])

    # ─────────────── handlers ───────────────

    def _register_handlers(self) -> None:
        assert self._dp is not None
        self._dp.message.register(self._on_start, CommandStart())
        self._dp.message.register(self._on_help, Command("help"))
        self._dp.message.register(self._on_balance, Command("balance"))
        self._dp.message.register(self._on_ref, Command("ref"))
        self._dp.message.register(self._on_catalog, Command("catalog"))
        self._dp.message.register(self._on_orders_stub, Command("orders"))
        self._dp.message.register(self._on_support_stub, Command("support"))

        # Callback'и для inline-навигации каталога.
        # cat:{cid}:{page}  — показать сервисы категории cid, страница page
        # svc:{sid}         — карточка сервиса sid
        # cats              — назад в список категорий
        # buy:{sid}         — заглушка покупки (откроется в Sprint 3)
        # close             — закрыть/удалить сообщение
        self._dp.callback_query.register(self._on_cb_cats, F.data == "cats")
        self._dp.callback_query.register(self._on_cb_close, F.data == "close")
        self._dp.callback_query.register(
            self._on_cb_category, F.data.startswith("cat:")
        )
        self._dp.callback_query.register(
            self._on_cb_service, F.data.startswith("svc:")
        )
        self._dp.callback_query.register(
            self._on_cb_buy_stub, F.data.startswith("buy:")
        )

    async def _on_start(self, message: Message, command: CommandObject) -> None:
        from_user = message.from_user
        if from_user is None:
            return

        # Парсим deep-link payload: /start ref_42 или /start 42
        ref_user_id = parse_referral_payload(command.args)

        async with session_factory()() as session:
            user, is_new = await get_or_create_user(
                session,
                telegram_user_id=from_user.id,
                telegram_username=from_user.username,
                first_name=from_user.first_name,
                language_code=from_user.language_code,
            )
            ref_attached = False
            if is_new and ref_user_id is not None and ref_user_id != user.id:
                ref = await attach_referral(
                    session,
                    referrer_user_id=ref_user_id,
                    referred_user_id=user.id,
                )
                ref_attached = ref is not None
            await session.commit()

        # Алерт в owner-бота: новая регистрация (не спамим репитами).
        if is_new and self._owner_notify is not None:
            try:
                username_disp = (
                    f"@{html.escape(from_user.username)}"
                    if from_user.username else "(без username)"
                )
                ref_note = (
                    f" по реф-ссылке от user_id={ref_user_id}"
                    if ref_attached else ""
                )
                await self._owner_notify(
                    f"🆕 Новый покупатель: {username_disp} "
                    f"(tg_id=<code>{from_user.id}</code>){ref_note}"
                )
            except Exception as exc:
                logger.debug(f"shop owner_notify on new user: {exc}")

        greeting_lines = [
            f"👋 Привет, <b>{html.escape(from_user.first_name or 'друг')}</b>!",
            "",
            "Это магазин подарочных карт (Apple, Steam, Spotify и другие).",
        ]
        if ref_attached:
            greeting_lines.append(
                "🎁 Ты пришёл по реферальной ссылке — друг будет получать 1% "
                "с каждой твоей покупки на свой внутренний баланс."
            )
        greeting_lines.extend([
            "",
            "<b>Что умеет бот</b>",
            "• <b>/catalog</b> — посмотреть товары",
            "• <b>/balance</b> — внутренний баланс (кэшбэк за рефералов)",
            "• <b>/ref</b> — твоя реф-ссылка",
            "• <b>/help</b> — все команды",
            "",
            "💡 <i>Магазин в режиме раннего доступа. Каталог и оплата откроются в ближайшие дни.</i>",
        ])
        await message.answer("\n".join(greeting_lines))

    async def _on_help(self, message: Message) -> None:
        await message.answer(HELP_TEXT)

    async def _on_balance(self, message: Message) -> None:
        from_user = message.from_user
        if from_user is None:
            return
        async with session_factory()() as session:
            user, _ = await get_or_create_user(
                session,
                telegram_user_id=from_user.id,
                telegram_username=from_user.username,
                first_name=from_user.first_name,
            )
            await session.commit()
        text = (
            f"💰 <b>Твой баланс:</b> {format_rub(user.balance_kopecks)}\n\n"
            "<i>Баланс пополняется на 1% от каждой покупки твоих рефералов "
            "и может использоваться для частичной/полной оплаты заказов в этом боте.</i>"
        )
        await message.answer(text)

    async def _on_ref(self, message: Message) -> None:
        from_user = message.from_user
        if from_user is None:
            return
        if self._username is None:
            await message.answer(
                "⏳ Ещё не готов — бот только запустился. Попробуй через минуту."
            )
            return
        async with session_factory()() as session:
            user, _ = await get_or_create_user(
                session,
                telegram_user_id=from_user.id,
                telegram_username=from_user.username,
                first_name=from_user.first_name,
            )
            await session.commit()
        ref_link = f"https://t.me/{self._username}?start=ref_{user.id}"
        text = (
            "🔗 <b>Твоя реферальная ссылка</b>\n\n"
            f"<code>{ref_link}</code>\n\n"
            "С каждой покупки приглашённого друга <b>1%</b> поступит на твой "
            "внутренний баланс. Балансом можно оплачивать заказы в этом боте."
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📤 Поделиться",
                                 url=f"https://t.me/share/url?url={ref_link}"
                                     f"&text=Магазин подарочных карт"),
        ]])
        await message.answer(text, reply_markup=markup)

    # ─────────────── /catalog + navigation ───────────────

    async def _on_catalog(self, message: Message) -> None:
        async with session_factory()() as session:
            cats = await list_categories_for_ui(session)
        text, markup = self._render_categories(cats)
        await message.answer(text, reply_markup=markup)

    async def _on_cb_cats(self, cb: CallbackQuery) -> None:
        async with session_factory()() as session:
            cats = await list_categories_for_ui(session)
        text, markup = self._render_categories(cats)
        await self._safe_edit(cb, text=text, markup=markup)

    async def _on_cb_category(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        if len(parts) < 2:
            await cb.answer()
            return
        try:
            cid = int(parts[1])
            page = int(parts[2]) if len(parts) >= 3 else 0
        except ValueError:
            await cb.answer("Битая ссылка")
            return
        offset = max(0, page) * CATALOG_PAGE_SIZE
        async with session_factory()() as session:
            rows, total = await list_services_in_category(
                session,
                category_id=cid,
                limit=CATALOG_PAGE_SIZE,
                offset=offset,
            )
        text, markup = self._render_services(
            category_id=cid,
            services=rows,
            total=total,
            page=page,
        )
        await self._safe_edit(cb, text=text, markup=markup)

    async def _on_cb_service(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        if len(parts) < 2:
            await cb.answer()
            return
        try:
            sid = int(parts[1])
        except ValueError:
            await cb.answer("Битая ссылка")
            return
        async with session_factory()() as session:
            svc = await get_catalog_service(session, ns_service_id=sid)
        if svc is None:
            await cb.answer("Товар временно недоступен", show_alert=True)
            return
        text, markup = self._render_service_card(svc)
        await self._safe_edit(cb, text=text, markup=markup)

    async def _on_cb_buy_stub(self, cb: CallbackQuery) -> None:
        """Покупка появится в Sprint 3 (CryptoBot) и Sprint 4 (Stars)."""
        await cb.answer(
            "💳 Оплата подключается. Скоро откроем!",
            show_alert=True,
        )

    async def _on_cb_close(self, cb: CallbackQuery) -> None:
        try:
            if cb.message is not None:
                await cb.message.delete()
        except Exception:
            pass
        await cb.answer()

    # ─────────────── renderers ───────────────

    @staticmethod
    def _render_categories(
        cats: list,
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        if not cats:
            return (
                "📭 <b>Каталог пока пуст.</b>\n\n"
                "Каталог обновляется автоматически — попробуй "
                "через 1–2 минуты.",
                None,
            )
        lines = ["🛍 <b>Каталог</b>", "", "Выбери категорию:"]
        rows = []
        for cat in cats:
            cheapest = format_rub(cat.cheapest_price_kopecks)
            label = (
                f"{cat.category_name} · {cat.services_count} · от {cheapest}"
            )
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"cat:{cat.category_id}:0",
                )
            ])
        rows.append([
            InlineKeyboardButton(text="✖ Закрыть", callback_data="close"),
        ])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _render_services(
        *,
        category_id: int,
        services: list,
        total: int,
        page: int,
    ) -> tuple[str, InlineKeyboardMarkup]:
        if not services:
            text = (
                "📭 В этой категории пока нечего показать.\n\n"
                "Возможно, временно нет в наличии — загляни чуть позже."
            )
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="« К категориям", callback_data="cats"),
            ]])
            return text, markup

        cat_name = services[0].category_name or "Без категории"
        total_pages = max(1, (total + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
        header = (
            f"🛍 <b>{html.escape(cat_name)}</b>\n"
            f"<i>стр. {page + 1} из {total_pages} · всего {total}</i>"
        )
        rows = []
        for svc in services:
            price = format_rub(svc.rub_price_kopecks)
            label = f"{svc.service_name} — {price}"
            if svc.in_stock < 5:
                label = f"⚠ {label} (мало: {svc.in_stock})"
            rows.append([
                InlineKeyboardButton(
                    text=label[:64],  # Telegram лимит на текст кнопки
                    callback_data=f"svc:{svc.ns_service_id}",
                )
            ])

        # Pagination
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="‹",
                callback_data=f"cat:{category_id}:{page - 1}",
            ))
        nav.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="noop",
        ))
        if (page + 1) * CATALOG_PAGE_SIZE < total:
            nav.append(InlineKeyboardButton(
                text="›",
                callback_data=f"cat:{category_id}:{page + 1}",
            ))
        if len(nav) > 1:  # не показываем pagination если только одна страница
            rows.append(nav)
        rows.append([
            InlineKeyboardButton(text="« К категориям", callback_data="cats"),
        ])
        return header, InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _render_service_card(svc) -> tuple[str, InlineKeyboardMarkup]:
        price = format_rub(svc.rub_price_kopecks)
        stock_line = (
            f"📦 В наличии: <b>{svc.in_stock}</b> шт."
            if svc.in_stock > 0
            else "🚫 <b>Нет в наличии</b>"
        )
        text = (
            f"🛒 <b>{html.escape(svc.service_name)}</b>\n"
            f"<i>{html.escape(svc.category_name or '')}</i>\n\n"
            f"💰 Цена: <b>{price}</b>\n"
            f"{stock_line}\n\n"
            "<i>Оплата откроется в ближайшие дни. "
            "Пока можно изучить ассортимент и поделиться с друзьями "
            "по реф-ссылке (/ref).</i>"
        )
        buy_label = "💳 Купить" if svc.in_stock > 0 else "🚫 Нет в наличии"
        rows = [
            [InlineKeyboardButton(
                text=buy_label,
                callback_data=(
                    f"buy:{svc.ns_service_id}"
                    if svc.in_stock > 0 else "noop"
                ),
            )],
            [
                InlineKeyboardButton(
                    text="« К категории",
                    callback_data=f"cat:{svc.category_id or 0}:0",
                ),
                InlineKeyboardButton(text="🏪 Все категории", callback_data="cats"),
            ],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    async def _safe_edit(
        cb: CallbackQuery, *, text: str, markup: InlineKeyboardMarkup | None
    ) -> None:
        """
        Редактирует сообщение, на котором нажали кнопку. Telegram бросает
        TelegramBadRequest('message is not modified') если text+markup
        совпадают с тем, что уже отображено — это безопасный шум, гасим.
        """
        try:
            if cb.message is not None:
                await cb.message.edit_text(text=text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug(f"shop edit_text: {exc}")
        await cb.answer()

    async def _on_orders_stub(self, message: Message) -> None:
        await message.answer(
            "📦 <b>Заказы будут здесь.</b>\n\n"
            "Когда оформишь первую покупку — увидишь её историю и пины."
        )

    async def _on_support_stub(self, message: Message) -> None:
        await message.answer(
            "🆘 <b>Поддержка</b>\n\n"
            "Напиши о проблеме в этот чат — оператор ответит лично. "
            "В период бета-доступа отвечаем в течение нескольких часов."
        )
