"""
Telegram-бот для покупателей (отдельный от owner-бота из src/alerts/bot.py).

Архитектура (Sprint 2.2 — Pro UX):
  - aiogram 3, long-polling, MemoryStorage FSM
  - persistent reply-меню как primary navigation (юзер не печатает команды,
    а тапает кнопки 🛍 Каталог / 🔍 Поиск / 💰 Баланс / 📦 Заказы / 👥 Рефералы / 🆘 Поддержка)
  - inline-keyboard внутри сообщений с пагинацией и хлебными крошками
  - FSM-flow поиска: тап → бот спрашивает фразу → выдаёт результаты
  - inline-query: `@bot apple` в любом чате — нативные подсказки Telegram
  - rate-limit на FSM-search (защита от brute-force)

Все клавиатуры/тексты — в src/shop/keyboards.py (pure-функции, тестируются
отдельно). Этот файл отвечает только за aiogram-маршрутизацию.

Не запускается если shop_enabled=False или shop_telegram_bot_token не задан —
silently no-op (можно деплоить код в прод без токена и включать тумблером в .env).
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import time
from types import SimpleNamespace
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)
from loguru import logger

from src.alerts.sessions import PaginationStore
from src.config import Settings, get_settings
from src.db.session import session_factory
from src.shop.keyboards import (
    BTN_BALANCE,
    BTN_CANCEL,
    BTN_CATALOG,
    BTN_ORDERS,
    BTN_REF,
    BTN_SEARCH,
    BTN_SUPPORT,
    CATALOG_GROUPS_PAGE_SIZE,
    SERVICES_PAGE_SIZE,
    cancel_keyboard,
    catalog_groups_keyboard,
    main_menu_keyboard,
    search_results_keyboard,
    service_card_keyboard,
    services_page_keyboard,
    variants_grid_keyboard,
)
from src.shop.repo import (
    apply_balance_change,
    attach_referral,
    get_catalog_service,
    get_or_create_user,
    list_categories_in_group,
    list_category_groups_for_ui,
    list_services_in_category,
    parse_referral_payload,
    search_services,
)
from src.shop.states import SearchState


# Сколько результатов поиска отдаём максимум (в FSM и inline-query).
SEARCH_MAX_RESULTS = 50

# Минимум между двумя последовательными /search от одного user'а (anti-spam).
SEARCH_RATE_LIMIT_SECONDS = 1.0


# Алерт-callback в owner-бота — для уведомлений о новых юзерах и пр.
OwnerNotify = Callable[[str], Awaitable[None]]


HELP_TEXT = (
    "🛒 <b>Магазин подарочных карт</b>\n\n"
    "Внизу — постоянное меню с разделами:\n"
    "• 🛍 <b>Каталог</b> — все товары по категориям\n"
    "• 🔍 <b>Поиск</b> — найти товар по названию\n"
    "• 💰 <b>Баланс</b> — твой внутренний баланс (кэшбэк от рефералов)\n"
    "• 📦 <b>Заказы</b> — история покупок\n"
    "• 👥 <b>Рефералы</b> — реф-ссылка, 1% с покупок друзей\n"
    "• 🆘 <b>Поддержка</b> — связаться с оператором\n\n"
    "<b>Команды</b>\n"
    "/start — главное меню\n"
    "/catalog — каталог\n"
    "/search <i>&lt;слово&gt;</i> — быстрый поиск\n"
    "/help — эта справка\n\n"
    "💎 <b>Inline-поиск:</b> в любом чате набери "
    "<code>@USERNAME слово</code> — увидишь подсказки прямо в Telegram "
    "(где <code>USERNAME</code> — имя этого бота).\n\n"
    "💡 <i>Магазин в режиме раннего доступа: оплата подключается в ближайшие дни.</i>"
)


def format_rub(kopecks: int) -> str:
    """1234500 копеек → '12 345 ₽' (полный формат с копейками)."""
    rub_int = kopecks // 100
    rub_frac = kopecks % 100
    groups = []
    s = str(rub_int)
    while s:
        groups.append(s[-3:])
        s = s[:-3]
    integer_part = "\u00a0".join(reversed(groups))
    if rub_frac:
        return f"{integer_part},{rub_frac:02d}\u00a0₽"
    return f"{integer_part}\u00a0₽"


def format_rub_compact(kopecks: int) -> str:
    """Компактный формат: округление до рубля. Для inline-кнопок и preview."""
    rub_int = (kopecks + 50) // 100
    groups = []
    s = str(rub_int)
    while s:
        groups.append(s[-3:])
        s = s[:-3]
    return "\u00a0".join(reversed(groups)) + "\u00a0₽"


class ShopBot:
    """
    Покупательский Telegram-бот для shop-витрины.

    Контракт жизненного цикла:
        bot = ShopBot(settings)
        await bot.start()      # не блокирует, запускает polling в task
        ...
        await bot.stop()       # graceful shutdown
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
        # Кэш результатов поиска: callback_data не вмещает запрос целиком.
        self._search_sessions: PaginationStore = PaginationStore()
        # Rate-limit для FSM-поиска: tg_user_id → timestamp последнего search.
        self._search_last_at: dict[int, float] = {}

    @property
    def enabled(self) -> bool:
        s = self._settings
        return s.shop_enabled and s.shop_telegram_bot_token is not None

    @property
    def username(self) -> str | None:
        """@username бота для реф-ссылок. None пока не стартовал."""
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
        # MemoryStorage хватит для MVP; для горизонтального масштабирования
        # подменить на RedisStorage без изменений в handler'ах.
        self._dp = Dispatcher(storage=MemoryStorage())
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
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="catalog", description="Каталог товаров"),
            BotCommand(command="search", description="Поиск по магазину"),
            BotCommand(command="balance", description="Мой баланс"),
            BotCommand(command="ref", description="Реф-ссылка"),
            BotCommand(command="orders", description="Мои заказы"),
            BotCommand(command="support", description="Поддержка"),
            BotCommand(command="help", description="Справка"),
        ])

    # ─────────────── handler registration ────

    def _register_handlers(self) -> None:
        assert self._dp is not None
        dp = self._dp

        # Команды (slash). Главное меню всегда висит — это для гиков/легаси.
        dp.message.register(self._on_start, CommandStart())
        dp.message.register(self._on_help, Command("help"))
        dp.message.register(self._on_catalog_cmd, Command("catalog"))
        dp.message.register(self._on_search_cmd, Command("search"))
        dp.message.register(self._on_balance_cmd, Command("balance"))
        dp.message.register(self._on_ref_cmd, Command("ref"))
        dp.message.register(self._on_orders_cmd, Command("orders"))
        dp.message.register(self._on_support_cmd, Command("support"))
        dp.message.register(
            self._on_cancel_cmd, Command("cancel"), StateFilter("*"),
        )

        # FSM: пользователь в состоянии «жду слово для поиска» — его сообщения
        # должны идти ИМЕННО сюда, обходя reply-button matchers ниже.
        dp.message.register(
            self._on_search_query, StateFilter(SearchState.waiting_for_query),
        )

        # Reply-buttons из главного меню (matches по точному тексту).
        # Регистрируем ПОСЛЕ FSM-хендлера, чтобы FSM выигрывал по приоритету.
        dp.message.register(self._on_catalog_cmd, F.text == BTN_CATALOG)
        dp.message.register(self._on_search_btn, F.text == BTN_SEARCH)
        dp.message.register(self._on_balance_cmd, F.text == BTN_BALANCE)
        dp.message.register(self._on_orders_cmd, F.text == BTN_ORDERS)
        dp.message.register(self._on_ref_cmd, F.text == BTN_REF)
        dp.message.register(self._on_support_cmd, F.text == BTN_SUPPORT)
        dp.message.register(self._on_cancel_cmd, F.text == BTN_CANCEL)

        # Callback'и для inline-навигации. Префикс → handler.
        # Порядок важен: более специфичные F.data == "x" регистрируем
        # ПЕРЕД startswith("x:"), иначе они никогда не сработают.
        dp.callback_query.register(self._on_cb_close, F.data == "close")
        dp.callback_query.register(self._on_cb_noop, F.data == "noop")
        dp.callback_query.register(
            self._on_cb_search_prompt, F.data == "search_prompt",
        )
        dp.callback_query.register(
            self._on_cb_cats, F.data.startswith("cats:"),
        )
        dp.callback_query.register(
            self._on_cb_group, F.data.startswith("grp:"),
        )
        dp.callback_query.register(
            self._on_cb_category, F.data.startswith("cat:"),
        )
        dp.callback_query.register(
            self._on_cb_service, F.data.startswith("svc:"),
        )
        dp.callback_query.register(
            self._on_cb_buy_stub, F.data.startswith("buy:"),
        )
        dp.callback_query.register(
            self._on_cb_search_page, F.data.startswith("srh:"),
        )

        # Inline-mode: @bot слово → нативные подсказки Telegram.
        dp.inline_query.register(self._on_inline_query)

    # ─────────────── /start ───────────────

    async def _on_start(self, message: Message, command: CommandObject) -> None:
        from_user = message.from_user
        if from_user is None:
            return

        # Парсим deep-link payload: /start ref_42 или /start 42.
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
            "🛒 <b>Магазин подарочных карт</b>",
            "Apple, Steam, Google Play, EA, Nintendo и ещё сотни товаров.",
        ]
        if ref_attached:
            greeting_lines.append(
                "\n🎁 Ты пришёл по реферальной ссылке — друг будет получать "
                "<b>1%</b> с каждой твоей покупки на свой баланс."
            )
        greeting_lines.extend([
            "",
            "✨ <b>Что нового:</b>",
            "• Удобный каталог с поиском",
            "• Прозрачные цены и копейки",
            "• Реферальная программа и кэшбэк",
            "• Поддержка 24/7",
            "",
            "👇 <i>Используй меню внизу, чтобы навигировать.</i>",
        ])
        await message.answer(
            "\n".join(greeting_lines),
            reply_markup=main_menu_keyboard(),
        )

    async def _on_help(self, message: Message) -> None:
        await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())

    # ─────────────── catalog ───────────────

    async def _on_catalog_cmd(self, message: Message, state: FSMContext) -> None:
        """Команда /catalog или reply-кнопка 🛍 Каталог."""
        await state.clear()
        async with session_factory()() as session:
            groups = await list_category_groups_for_ui(session)
        text, markup = catalog_groups_keyboard(groups=groups, page=0)
        await message.answer(text, reply_markup=markup)

    async def _on_cb_cats(self, cb: CallbackQuery) -> None:
        """cats:{page} — обновить экран каталога с пагинацией."""
        page = self._parse_int_or(cb.data, idx=1, default=0)
        async with session_factory()() as session:
            groups = await list_category_groups_for_ui(session)
        text, markup = catalog_groups_keyboard(groups=groups, page=page)
        await self._safe_edit(cb, text=text, markup=markup)

    async def _on_cb_group(self, cb: CallbackQuery) -> None:
        """grp:{slug} — drill-down в группу. Если 1 вариант — сразу к услугам."""
        parts = (cb.data or "").split(":")
        if len(parts) < 2:
            await cb.answer()
            return
        slug = parts[1]
        async with session_factory()() as session:
            variants = await list_categories_in_group(session, group_slug=slug)
        if not variants:
            await cb.answer("Группа пуста или временно недоступна", show_alert=True)
            return
        if len(variants) == 1:
            await self._show_services_for_cb(
                cb, category_id=variants[0].category_id, page=0,
                group_slug=slug,
            )
            return
        # base_name берём из репозитория категории (parse_category_name).
        first_name = variants[0].category_name
        base = first_name.split("|", 1)[0].strip() if "|" in first_name else first_name
        text, markup = variants_grid_keyboard(variants=variants, base_name=base)
        await self._safe_edit(cb, text=text, markup=markup)

    async def _on_cb_category(self, cb: CallbackQuery) -> None:
        """cat:{cid}:{page} — список услуг внутри NS-категории."""
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
        # Выясняем group_slug этой категории для кнопки «назад к группе».
        async with session_factory()() as session:
            services_for_meta, _ = await list_services_in_category(
                session, category_id=cid, limit=1, offset=0,
            )
        group_slug = (
            services_for_meta[0].group_slug if services_for_meta else None
        )
        await self._show_services_for_cb(
            cb, category_id=cid, page=page, group_slug=group_slug,
        )

    async def _show_services_for_cb(
        self,
        cb: CallbackQuery,
        *,
        category_id: int,
        page: int,
        group_slug: str | None,
    ) -> None:
        offset = max(0, page) * SERVICES_PAGE_SIZE
        async with session_factory()() as session:
            rows, total = await list_services_in_category(
                session,
                category_id=category_id,
                limit=SERVICES_PAGE_SIZE,
                offset=offset,
            )
        text, markup = services_page_keyboard(
            services=rows, total=total,
            category_id=category_id, page=page,
            group_slug=group_slug,
        )
        await self._safe_edit(cb, text=text, markup=markup)

    async def _on_cb_service(self, cb: CallbackQuery) -> None:
        """svc:{sid} — карточка услуги."""
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
        group_slug = getattr(svc, "group_slug", None)
        text, markup = service_card_keyboard(svc=svc, group_slug=group_slug)
        await self._safe_edit(cb, text=text, markup=markup)

    async def _on_cb_buy_stub(self, cb: CallbackQuery) -> None:
        """Покупка появится в Sprint 3 (CryptoBot)."""
        await cb.answer(
            "💳 Оплата подключается в ближайшие дни. Скоро откроем!",
            show_alert=True,
        )

    async def _on_cb_close(self, cb: CallbackQuery) -> None:
        try:
            if cb.message is not None:
                await cb.message.delete()
        except Exception:
            pass
        await cb.answer()

    async def _on_cb_noop(self, cb: CallbackQuery) -> None:
        await cb.answer()

    # ─────────────── search ───────────────

    async def _on_cb_search_prompt(self, cb: CallbackQuery) -> None:
        """Кнопка «🔍 Поиск» в каталоге — открывает FSM-flow."""
        await cb.answer()
        if cb.message is None or cb.from_user is None:
            return
        # Откроем поиск в новом сообщении, чтобы оставить экран каталога.
        await self._enter_search_state(
            chat_id=cb.message.chat.id, user_id=cb.from_user.id,
        )

    async def _on_search_btn(self, message: Message, state: FSMContext) -> None:
        """Reply-кнопка 🔍 Поиск в главном меню."""
        if message.from_user is None:
            return
        await self._enter_search_state_for_message(message, state)

    async def _on_search_cmd(
        self, message: Message, command: CommandObject, state: FSMContext,
    ) -> None:
        """
        /search [запрос]:
        - /search apple → сразу показать результаты (для гиков и deep-links);
        - /search → войти в FSM (для обычных юзеров).
        """
        query = (command.args or "").strip()
        if query:
            await state.clear()
            await self._do_search_and_reply(message, query=query)
            return
        await self._enter_search_state_for_message(message, state)

    async def _enter_search_state_for_message(
        self, message: Message, state: FSMContext
    ) -> None:
        if message.from_user is None:
            return
        await state.set_state(SearchState.waiting_for_query)
        await message.answer(
            "🔍 <b>Поиск по магазину</b>\n\n"
            "Напиши слово или фразу — найду подходящие товары.\n"
            "Например: <code>apple</code>, <code>ea sports</code>, <code>steam</code>.\n\n"
            "<i>Чтобы выйти — нажми «Отмена».</i>",
            reply_markup=cancel_keyboard(),
        )

    async def _enter_search_state(self, *, chat_id: int, user_id: int) -> None:
        """Альтернативный вход в FSM из callback'а (без объекта Message FSM-юзера)."""
        assert self._bot is not None and self._dp is not None
        # Aiogram FSM key: (bot_id, chat_id, user_id).
        from aiogram.fsm.storage.base import StorageKey
        key = StorageKey(
            bot_id=self._bot.id, chat_id=chat_id, user_id=user_id,
        )
        await self._dp.storage.set_state(key, SearchState.waiting_for_query)
        await self._bot.send_message(
            chat_id,
            "🔍 <b>Поиск по магазину</b>\n\n"
            "Напиши слово или фразу — найду подходящие товары.\n\n"
            "<i>Чтобы выйти — нажми «Отмена».</i>",
            reply_markup=cancel_keyboard(),
        )

    async def _on_search_query(
        self, message: Message, state: FSMContext,
    ) -> None:
        """Пользователь в SearchState.waiting_for_query пишет текст."""
        if message.from_user is None:
            return
        # Кнопка «Отмена» в reply-keyboard приходит как обычный текст
        # и должна была быть обработана раньше matcher'ом BTN_CANCEL,
        # но FSM-хендлер регистрируется первым → проверим явно.
        if message.text and message.text.strip() == BTN_CANCEL:
            await self._on_cancel_cmd(message, state)
            return

        # Rate-limit: 1 поиск/сек на user_id.
        now = time.monotonic()
        last = self._search_last_at.get(message.from_user.id, 0.0)
        if now - last < SEARCH_RATE_LIMIT_SECONDS:
            await message.answer("⏳ Слишком быстро — подожди секунду.")
            return
        self._search_last_at[message.from_user.id] = now

        query = (message.text or "").strip()
        if len(query) < 2:
            await message.answer(
                "Слишком короткий запрос — введи минимум 2 символа.",
            )
            return

        await state.clear()
        await self._do_search_and_reply(message, query=query)

    async def _do_search_and_reply(self, message: Message, *, query: str) -> None:
        async with session_factory()() as session:
            results = await search_services(
                session, query=query, limit=SEARCH_MAX_RESULTS,
            )

        if not results:
            await message.answer(
                f"🔍 По запросу <b>«{html.escape(query)}»</b> ничего не нашлось.\n\n"
                "Попробуй другое слово или загляни в 🛍 Каталог.",
                reply_markup=main_menu_keyboard(),
            )
            return

        # Сохраняем results в session-store по короткому id, чтобы пагинация
        # шла без повторного похода в БД и без долгого callback_data.
        sid = self._search_sessions.put(
            items=[self._serialize_svc_for_cache(r) for r in results],
            title=query,
        )
        text, markup = search_results_keyboard(
            page_items=results[:SERVICES_PAGE_SIZE],
            total=len(results), page=0, session_id=sid,
            query=query, truncated_at=SEARCH_MAX_RESULTS,
        )
        await message.answer(
            text,
            reply_markup=markup,
        )
        # Восстановим главное меню (если до этого был cancel_keyboard).
        await message.answer(
            "Готово. Можешь продолжить навигацию через меню внизу.",
            reply_markup=main_menu_keyboard(),
        )

    async def _on_cb_search_page(self, cb: CallbackQuery) -> None:
        """srh:{sid}:{page} — пагинация в результатах поиска."""
        parts = (cb.data or "").split(":")
        if len(parts) < 3:
            await cb.answer()
            return
        sid, raw_page = parts[1], parts[2]
        try:
            page = int(raw_page)
        except ValueError:
            await cb.answer()
            return
        sess = self._search_sessions.get(sid)
        if sess is None:
            await cb.answer(
                "Поиск устарел. Открой 🔍 Поиск и попробуй снова.",
                show_alert=True,
            )
            return
        total = len(sess.items)
        start = max(0, page) * SERVICES_PAGE_SIZE
        page_items_raw = sess.items[start : start + SERVICES_PAGE_SIZE]
        page_items = [self._deserialize_svc(row) for row in page_items_raw]
        text, markup = search_results_keyboard(
            page_items=page_items, total=total, page=page,
            session_id=sid, query=sess.title,
            truncated_at=SEARCH_MAX_RESULTS,
        )
        await self._safe_edit(cb, text=text, markup=markup)

    @staticmethod
    def _serialize_svc_for_cache(svc) -> tuple:
        """Сериализуем ShopCatalogCache в tuple для PaginationStore.
        Tuple дёшево хешится и не тянет SQLAlchemy session."""
        return (
            svc.ns_service_id, svc.service_name, svc.rub_price_kopecks,
            svc.in_stock, svc.category_name,
        )

    @staticmethod
    def _deserialize_svc(row: tuple) -> SimpleNamespace:
        return SimpleNamespace(
            ns_service_id=row[0],
            service_name=row[1],
            rub_price_kopecks=row[2],
            in_stock=row[3],
            category_name=row[4],
        )

    # ─────────────── inline-query (@bot слово) ───────────────

    async def _on_inline_query(self, q: InlineQuery) -> None:
        """
        Нативный inline-search Telegram: пользователь пишет `@MyBot apple`
        в любом чате — получает выпадающий список товаров с превью.

        Тап на результат → вставляет в чат текст с реф-ссылкой бота
        (открывается сразу карточка товара через deep-link).
        """
        if self._bot is None:
            return
        query = (q.query or "").strip()
        if len(query) < 2:
            try:
                await q.answer(results=[], cache_time=1, is_personal=True)
            except Exception:
                pass
            return

        async with session_factory()() as session:
            results = await search_services(session, query=query, limit=25)

        articles = []
        for svc in results:
            price = format_rub_compact(svc.rub_price_kopecks)
            title = f"{svc.service_name} — {price}"
            desc = svc.category_name or ""
            if svc.in_stock < 5:
                desc = f"⚠ Осталось: {svc.in_stock} · {desc}"
            else:
                desc = f"В наличии · {desc}"
            # Deep-link открывает наш бот и автоматически шлёт /start svc_{id}.
            # Для MVP — просто текст с upsell'ом.
            content = InputTextMessageContent(
                message_text=(
                    f"🛒 <b>{html.escape(svc.service_name)}</b>\n"
                    f"💰 {price}\n\n"
                    f"Купить в боте: https://t.me/{self._username}"
                ),
                parse_mode=ParseMode.HTML,
            )
            articles.append(InlineQueryResultArticle(
                # Telegram требует уникальный id ≤64 байт.
                id=hashlib.md5(
                    f"{svc.ns_service_id}".encode()
                ).hexdigest()[:16],
                title=title[:64],
                description=desc[:128],
                input_message_content=content,
            ))
        try:
            await q.answer(
                results=articles,
                cache_time=15,  # Telegram кэширует один и тот же query на 15с
                is_personal=False,
            )
        except Exception as exc:
            logger.debug(f"shop inline_query answer: {exc}")

    # ─────────────── balance / ref / orders / support ───────────────

    async def _on_balance_cmd(self, message: Message, state: FSMContext) -> None:
        await state.clear()
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
            "<i>Баланс пополняется на 1% от покупок твоих рефералов и "
            "может использоваться для оплаты заказов в этом боте.</i>"
        )
        await message.answer(text, reply_markup=main_menu_keyboard())

    async def _on_ref_cmd(self, message: Message, state: FSMContext) -> None:
        await state.clear()
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
            "👥 <b>Реферальная программа</b>\n\n"
            "Делись ссылкой — получай <b>1%</b> с каждой покупки приглашённого "
            "друга на свой внутренний баланс. Балансом можно оплачивать заказы.\n\n"
            f"🔗 <b>Твоя ссылка:</b>\n<code>{ref_link}</code>"
        )
        share_url = (
            f"https://t.me/share/url?url={ref_link}"
            f"&text=%F0%9F%9B%92%20%D0%9C%D0%B0%D0%B3%D0%B0%D0%B7%D0%B8%D0%BD"
            "%20%D0%BF%D0%BE%D0%B4%D0%B0%D1%80%D0%BE%D1%87%D0%BD%D1%8B%D1%85"
            "%20%D0%BA%D0%B0%D1%80%D1%82"
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", url=share_url)],
        ])
        await message.answer(text, reply_markup=markup)
        await message.answer(
            "<i>Готово — лови приглашённых через 💰 Баланс.</i>",
            reply_markup=main_menu_keyboard(),
        )

    async def _on_orders_cmd(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "📦 <b>Мои заказы</b>\n\n"
            "Когда оформишь первую покупку — увидишь её здесь: статус, "
            "пины, дату. Сейчас раздел пуст.",
            reply_markup=main_menu_keyboard(),
        )

    async def _on_support_cmd(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "🆘 <b>Поддержка</b>\n\n"
            "Опиши проблему в этот чат — оператор увидит сообщение и ответит "
            "лично. В период бета-доступа отвечаем в течение нескольких часов.\n\n"
            "<b>Часто спрашивают</b>\n"
            "• <i>Когда откроется оплата?</i> — в ближайшие дни (CryptoBot и "
            "Telegram Stars).\n"
            "• <i>Безопасно ли?</i> — да: оплата через официальные шлюзы "
            "Telegram, доставка моментальная.\n"
            "• <i>Что с кэшбэком?</i> — 1% от покупок друзей идёт на твой баланс.",
            reply_markup=main_menu_keyboard(),
        )

    async def _on_cancel_cmd(
        self, message: Message, state: FSMContext,
    ) -> None:
        await state.clear()
        await message.answer(
            "Окей, отменил. Чем ещё помочь?",
            reply_markup=main_menu_keyboard(),
        )

    # ─────────────── helpers ───────────────

    @staticmethod
    def _parse_int_or(data: str | None, *, idx: int, default: int) -> int:
        if not data:
            return default
        parts = data.split(":")
        if len(parts) <= idx:
            return default
        try:
            return int(parts[idx])
        except ValueError:
            return default

    @staticmethod
    async def _safe_edit(
        cb: CallbackQuery,
        *,
        text: str,
        markup: InlineKeyboardMarkup | None,
    ) -> None:
        """
        Редактирует сообщение, на котором нажали кнопку. Telegram кидает
        TelegramBadRequest('message is not modified') если text+markup
        совпадают — это безопасный шум, гасим.
        """
        try:
            if cb.message is not None:
                await cb.message.edit_text(text=text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug(f"shop edit_text: {exc}")
        await cb.answer()
