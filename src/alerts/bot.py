"""
Интерактивный Telegram-бот на aiogram 3.x.

Архитектура:
    - Команды (Command) — тонкие хэндлеры, делают валидацию и вызывают _do_*.
    - Callback-router (callback_query) — обрабатывает inline-кнопки.
    - PaginationStore (src.alerts.sessions) — хранит результаты для листания.
    - ui.py — фабрики клавиатур и форматтеры карточек.

Авторизация: владельцем считается chat_id == TELEGRAM_CHAT_ID из .env.
Команды /start, /ping, /version, /whoami отвечают всем — это безопасные
пробники. Остальные доступны только владельцу.
"""
from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from types import SimpleNamespace
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from loguru import logger
from sqlalchemy import desc, func, select

from src.alerts import ui
from src.alerts.sessions import PAGE_SIZE, PaginationStore, paginate
from src.config import Settings, get_settings
from src.db.models import KnownLot, LotGroup, Mapping, Order, SyncRun
from src.db.repo import (
    assign_mapping_group,
    classify_lot_group,
    find_order_by_funpay_id,
    list_lot_groups,
    list_mappings,
    list_pending_confirmation,
    upsert_mapping,
)
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.orders.sync_paid import sync_pending_confirmation
from src.mapping.rules import compute_pricing, estimate_profit_rub
from src.mapping.safety import mapping_risk_warnings
from src.ns import NSClient
from src.ns.models import StockResponse
from src.sync.fx import get_rate_breakdown


SyncTrigger = Callable[[], Awaitable[dict]]
FunPayReconnect = Callable[[], Awaitable[dict]]
OrderRetry = Callable[[str], Awaitable[dict]]

# Таймауты: если NS/FunPay завис — бот не должен молчать вечно.
NS_TIMEOUT_SECONDS = 15.0
FP_TIMEOUT_SECONDS = 15.0


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _to_moscow(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MOSCOW_TZ)


def _format_dt(dt: datetime | None) -> str:
    msk = _to_moscow(dt)
    return "—" if msk is None else msk.strftime("%Y-%m-%d %H:%M:%S")


def _split_lines_to_chunks(
    *,
    header_lines: list[str],
    body_lines: list[str],
    max_chars: int = 3500,
) -> list[str]:
    """
    Склеить header + body в чанки ≤ max_chars (запас под Telegram 4096).

    Гарантии:
    - каждый чанк начинается с header (чтобы юзер не терялся);
    - ни одна строка не режется посередине;
    - если одна строка длиннее max_chars (теоретически невозможно для
      наших данных — order_id 8 chars + username 24 chars + 30 chars текста),
      она всё равно отправится одной строкой, даже превысив лимит
      (Telegram отрежет на 4096, но это лучше, чем silently потерять
      запись из списка).
    """
    if not body_lines:
        return ["\n".join(header_lines)] if header_lines else []
    header_text = "\n".join(header_lines)
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = len(header_text) + 1  # +1 на \n после header
    for line in body_lines:
        line_len = len(line) + 1
        if current_lines and current_len + line_len > max_chars:
            chunks.append(header_text + "\n" + "\n".join(current_lines))
            current_lines = [line]
            current_len = len(header_text) + 1 + line_len
        else:
            current_lines.append(line)
            current_len += line_len
    if current_lines:
        chunks.append(header_text + "\n" + "\n".join(current_lines))
    return chunks


def _split_ids_to_copy_chunks(
    ids: list[str],
    *,
    max_chars: int = 3500,
) -> list[str]:
    """
    Склеить order_ids в строки «#ID, #ID, ...» с ограничением по длине.
    Используется для блока «скопировать в саппорт».
    """
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    SEP = ", "
    for oid in ids:
        token = f"#{oid}"
        add_len = len(token) + (len(SEP) if current else 0)
        if current and current_len + add_len > max_chars:
            chunks.append(SEP.join(current))
            current = [token]
            current_len = len(token)
        else:
            current.append(token)
            current_len += add_len
    if current:
        chunks.append(SEP.join(current))
    return chunks


def _parse_hours_arg(command_text: str | None, *, default: int) -> int:
    """
    Достать число часов из текста команды вида `/pending_confirm 12`.
    Возвращает default если аргумента нет или он некорректен.
    Clamping: 1..168 (1ч..1неделя), защита от очепяток вроде 100000.
    """
    if not command_text:
        return default
    parts = command_text.strip().split()
    if len(parts) < 2:
        return default
    try:
        hours = int(parts[1])
    except (ValueError, TypeError):
        return default
    if hours < 1:
        return 1
    if hours > 168:
        return 168
    return hours


def format_percent(value: float | int | None) -> str:
    """
    Красивое отображение процентов: целые без «.0», дробные — до 2 знаков
    с обрезкой хвостовых нулей. Например: 6 -> '6', 5.5 -> '5.5', 5.50 -> '5.5'.
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}"
    text = f"{v:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_order_line(o: Order) -> str:
    created_at = _to_moscow(o.created_at)
    created_text = "—" if created_at is None else created_at.strftime("%m-%d %H:%M")
    return (
        f"<code>{created_text} MSK</code> "
        f"#{o.funpay_order_id} → {o.status} "
        f"(NS:{o.ns_custom_id or '—'})"
    )


def _format_problem_line(o: Order) -> str:
    created_at = _to_moscow(o.updated_at or o.created_at)
    created_text = "—" if created_at is None else created_at.strftime("%m-%d %H:%M")
    error = (o.error or "").replace("\n", " ")
    error_text = f"\n   <code>{html.escape(error[:120])}</code>" if error else ""
    return (
        f"<code>{created_text} MSK</code> "
        f"#{html.escape(o.funpay_order_id)} · <b>{html.escape(o.status)}</b> "
        f"· lot <code>{o.funpay_lot_id}</code>{error_text}"
    )


def _format_status_text(
    settings: Settings,
    last_run: SyncRun | None,
    ns_balance: str,
    fp_status: str,
    rate_line: str,
) -> str:
    lines = [
        "📊 <b>NS↔FunPay Bridge — статус</b>",
        "",
    ]
    if settings.enable_real_actions:
        lines.append("⚙ Real actions: <b>🟢 ON</b>")
    else:
        lines.append(
            "⚙ Real actions: <b>🔴 OFF (dry-run)</b>\n"
            "❗ <b>Цены НЕ обновляются на FunPay</b>. "
            "Поставь <code>ENABLE_REAL_ACTIONS=true</code> в .env и "
            "<code>systemctl restart funpay-ns-bot</code>."
        )
    lines.extend([
        f"⏱ Sync каждые: <b>{settings.sync_interval_seconds}c</b>",
        f"💱 Валюта FunPay: <b>{settings.funpay_currency.value}</b>",
        f"📈 Наценка по умолчанию: <b>{settings.markup_percent}%</b>",
        rate_line,
        f"💰 Баланс NS: <b>{ns_balance}</b>",
        f"🔌 FunPay: <b>{fp_status}</b>",
    ])
    if last_run is not None:
        lines.extend([
            "",
            "<b>Последний sync:</b>",
            f"  начат: {_format_dt(last_run.started_at)}",
            f"  завершён: {_format_dt(last_run.finished_at)}",
            f"  статус: <b>{last_run.status}</b>",
            f"  checked/updated/skipped: "
            f"{last_run.lots_checked}/{last_run.lots_updated}/{last_run.lots_skipped}",
        ])
        if last_run.error:
            lines.append(f"  ошибка: <code>{html.escape(last_run.error[:200])}</code>")
    else:
        lines.append("")
        lines.append("<i>Sync ещё не запускался</i>")
    return "\n".join(lines)


def _read_build_info() -> str:
    """
    Возвращает короткую строку с версией задеплоенного кода — git SHA и
    дату последнего коммита.

    Приоритет источников:
    1. src/_version.py — пишется deploy/stamp_version.py перед каждым push'ем.
    2. BUILD_INFO — пишется deploy/fetch_code.sh, если api.github.com доступен.
    """
    # 1. _version.py — самый надёжный путь
    try:
        from src import _version  # type: ignore
        sha = (getattr(_version, "SHA", "") or "")[:12]
        date = getattr(_version, "DATE", "") or ""
        subj = (getattr(_version, "SUBJECT", "") or "")[:80]
        if sha and sha != "unknown":
            return f"build: <code>{sha}</code> ({date})\nкоммит: <i>{subj}</i>\n"
    except Exception:
        pass

    # 2. BUILD_INFO fallback
    import os
    candidates = [
        os.path.join(os.getcwd(), "BUILD_INFO"),
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..",
            "BUILD_INFO",
        ),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = dict(
                    line.strip().split("=", 1)
                    for line in f if "=" in line
                )
        except OSError:
            continue
        sha = (data.get("sha") or "")[:12]
        date = data.get("date") or ""
        subj = (data.get("subject") or "")[:80]
        if sha:
            return f"build: <code>{sha}</code> ({date})\nкоммит: <i>{subj}</i>\n"
    return "build: <i>версия не определена</i>\n"


def _guard(handler):
    """Декоратор: ловит исключения внутри хэндлера и шлёт юзеру внятный ответ."""

    @wraps(handler)
    async def wrapper(self, event, *args, **kwargs):
        try:
            return await handler(self, event, *args, **kwargs)
        except Exception as exc:
            logger.exception(f"Handler {handler.__name__} упал: {exc}")
            text = (
                f"⚠ Внутренняя ошибка в <code>{handler.__name__}</code>:\n"
                f"<code>{html.escape(str(exc))[:300]}</code>"
            )
            if isinstance(event, CallbackQuery):
                with suppress_telegram():
                    await event.answer("Ошибка, см. сообщение", show_alert=False)
                if event.message is not None:
                    with suppress_telegram():
                        await event.message.answer(text)
            elif isinstance(event, Message):
                with suppress_telegram():
                    await event.answer(text)
            return None

    return wrapper


class suppress_telegram:
    """Контекст-менеджер: глотает TelegramBadRequest (например, "message not modified")."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, (TelegramBadRequest,))


class TelegramBot:
    HELP_TEXT = (
        "🤖 <b>NS↔FunPay Bridge — команды</b>\n\n"
        "<b>Меню и состояние</b>\n"
        "/menu — главное меню с кнопками\n"
        "/status — общий обзор\n"
        "/balance — баланс NS и FunPay\n"
        "/orders — последние 10 заказов\n"
        "/pending_confirm [часы] — заказы старше Nч без подтверждения (для саппорта)\n"
        "/sync_pending_confirm — синхронизировать /pending_confirm с FunPay (закрыть тихо подтверждённые)\n"
        "/sync — запустить синхронизацию\n"
        "/funpay_reconnect — переподключить FunPay\n"
        "\n"
        "<b>Каталог NS</b>\n"
        "/ns_search &lt;слово&gt; — поиск по названию услуги\n"
        "/ns_cats — список категорий NS\n"
        "\n"
        "<b>Лоты и маппинги</b>\n"
        "/lots — мои лоты на FunPay\n"
        "/mappings — текущие маппинги\n"
        "/map &lt;funpay_lot_id&gt; &lt;ns_service_id&gt; [markup%] [label]\n"
        "/unmap &lt;funpay_lot_id&gt;\n"
        "/setmarkup &lt;funpay_lot_id&gt; &lt;percent|default&gt;\n"
        "/reset_markups [percent] — сбросить индивидуальные наценки\n"
        "/clear_target — забыть выбранный целевой лот\n"
        "/calc &lt;funpay_lot_id&gt; — посчитать цены по маппингу\n"
        "/inspect_lot &lt;funpay_lot_id&gt; — заглянуть в LotFields\n"
        "/lot_status &lt;funpay_lot_id&gt; — read-only диагностика: cache, capped, target\n"
        "\n"
        "<b>Глобальные настройки (без рестарта)</b>\n"
        "/settings — показать активные значения\n"
        "/setdefault markup &lt;%|default&gt; — глобальная наценка (FunPay)\n"
        "/setdefault premium &lt;%|default&gt; — премия к курсу USD\n"
        "/setdefault stockcap &lt;N|default&gt; — лимит остатков на FunPay\n"
        "/setdefault shop_markup &lt;%|default&gt; — наценка TG-shop\n"
        "/setdefault shop_referral &lt;%|default&gt; — % кэшбэка реф-системы\n"
        "/force_sync &lt;funpay_lot_id&gt; — диагностика одного лота\n"
        "/funpay_check — проверить FunPay-сессию (cookies)\n"
        "\n"
        "<b>Сервисные</b>\n"
        "/ping — проверка связи\n"
        "/version — версия + chat_id\n"
        "/whoami — твой chat_id\n"
        "/help — это сообщение"
    )

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sync_trigger: SyncTrigger | None = None,
        funpay_client: FunPayClient | None = None,
        funpay_reconnect: FunPayReconnect | None = None,
        order_retry: OrderRetry | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sync_trigger = sync_trigger
        self._funpay_client = funpay_client
        self._funpay_reconnect = funpay_reconnect
        self._order_retry = order_retry
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._task: asyncio.Task | None = None
        self._sessions = PaginationStore()
        # chat_id -> funpay_lot_id, который выбран как target для маппинга
        self._target_lots: dict[int, int] = {}
        # chat_id -> человеческая подпись лота для подсказок
        self._target_labels: dict[int, str] = {}
        # chat_id -> id "панели управления" в этом чате; при новой команде
        # старая удаляется, чтобы не плодить дубликаты сообщений-меню.
        self._control_msg: dict[int, int] = {}
        # кэш каталога NS на 60 секунд, чтобы не дёргать API на каждый клик
        self._stock_cache: tuple[float, StockResponse] | None = None
        self._stock_lock = asyncio.Lock()

    def update_funpay_client(self, fp: FunPayClient | None) -> None:
        self._funpay_client = fp

    @property
    def enabled(self) -> bool:
        s = self._settings
        return s.telegram_enabled and s.telegram_bot_token is not None

    # ─────────────── lifecycle ───────────────

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Telegram-бот отключён (TELEGRAM_ENABLED=false или нет токена)")
            return

        token = self._settings.telegram_bot_token.get_secret_value()  # type: ignore[union-attr]
        self._bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()
        self._register_handlers()

        # Дренируем старые апдейты и регистрируем меню команд в клиенте
        try:
            await self._bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            logger.debug(f"delete_webhook: {exc}")
        try:
            await self._set_bot_commands()
        except Exception as exc:
            logger.debug(f"set_my_commands: {exc}")

        me = await self._bot.get_me()
        logger.info(f"Telegram-бот @{me.username} стартовал (long-polling)")
        self._task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="telegram-bot-polling",
        )

    async def stop(self) -> None:
        if self._dp is not None:
            await self._dp.stop_polling()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        if self._bot is not None:
            await self._bot.session.close()
        logger.info("Telegram-бот остановлен")

    async def _set_bot_commands(self) -> None:
        """Регистрируем команды в меню Telegram (всплывает по '/' )."""
        from aiogram.types import BotCommand

        cmds = [
            BotCommand(command="menu", description="🏠 Главное меню"),
            BotCommand(command="status", description="📊 Статус"),
            BotCommand(command="balance", description="💰 Балансы"),
            BotCommand(command="lots", description="🛒 Лоты FunPay"),
            BotCommand(command="mappings", description="🗺 Маппинги"),
            BotCommand(command="ns_cats", description="🗂 Каталог NS"),
            BotCommand(command="ns_search", description="🔍 Поиск NS"),
            BotCommand(command="sync", description="🔄 Синхронизация"),
            BotCommand(command="orders", description="📦 Последние заказы"),
            BotCommand(command="pending_confirm", description="⏳ Заказы без подтв. (список для саппорта FunPay)"),
            BotCommand(command="sync_pending_confirm", description="🔄 Sync /pending_confirm с FunPay (чистит фантомы)"),
            BotCommand(command="setmarkup", description="✏ Наценка одного маппинга"),
            BotCommand(command="reset_markups", description="♻ Сбросить наценку у всех маппингов"),
            BotCommand(command="setdefault", description="🎚 Глобальные настройки (markup/premium/stock)"),
            BotCommand(command="settings", description="🔧 Показать текущие настройки"),
            BotCommand(command="force_sync", description="🔬 Прогнать один лот с деталями"),
            BotCommand(command="lot_status", description="🩹 Read-only: cache/capped/target лота"),
            BotCommand(command="funpay_check", description="🩺 Проверить FunPay-сессию"),
            BotCommand(command="funpay_reconnect", description="🔌 FunPay reconnect"),
            BotCommand(command="ping", description="🏓 Проверка связи"),
            BotCommand(command="help", description="❓ Помощь"),
        ]
        if self._bot is not None:
            await self._bot.set_my_commands(cmds)

    # ─────────────── авторизация ───────────────

    def _is_owner(self, msg_or_cq: Message | CallbackQuery) -> bool:
        owner = self._settings.telegram_chat_id
        if owner is None:
            return False
        chat_id = (
            msg_or_cq.from_user.id
            if isinstance(msg_or_cq, CallbackQuery)
            else msg_or_cq.chat.id
        )
        return chat_id == owner

    # ─────────────── регистрация хэндлеров ───────────────

    def _register_handlers(self) -> None:
        dp = self._dp
        assert dp is not None

        # ----- безопасные команды (всем) -----

        @dp.message(CommandStart())
        async def cmd_start(msg: Message) -> None:
            await self._on_start(msg)

        @dp.message(Command("ping"))
        async def cmd_ping(msg: Message) -> None:
            await msg.answer(
                f"🏓 pong (chat_id=<code>{msg.chat.id}</code>)\n"
                f"long-polling работает."
            )

        @dp.message(Command("version"))
        async def cmd_version(msg: Message) -> None:
            owner = self._settings.telegram_chat_id
            owner_text = (
                "<i>не задан в .env</i>" if owner is None else f"<code>{owner}</code>"
            )
            access = (
                "✅ ты владелец"
                if self._is_owner(msg)
                else "❌ ты НЕ владелец — большинство команд проигнорирую"
            )
            build_line = _read_build_info()
            await msg.answer(
                f"🤖 <b>NS↔FunPay Bridge</b>\n"
                f"{build_line}"
                f"real_actions: <b>{self._settings.enable_real_actions}</b>\n"
                f"timezone: <b>{self._settings.timezone}</b>\n"
                f"TELEGRAM_CHAT_ID: {owner_text}\n"
                f"твой chat_id: <code>{msg.chat.id}</code>\n"
                f"{access}"
            )

        @dp.message(Command("whoami"))
        async def cmd_whoami(msg: Message) -> None:
            await msg.answer(f"chat_id = <code>{msg.chat.id}</code>")

        # ----- команды только для владельца -----

        @dp.message(Command("help"))
        async def cmd_help(msg: Message) -> None:
            if not self._is_owner(msg):
                await msg.answer(
                    "Я отвечаю на команды только своему владельцу.\n"
                    f"Твой chat_id: <code>{msg.chat.id}</code>. "
                    "Впиши его в .env как <code>TELEGRAM_CHAT_ID</code> "
                    "и перезапусти сервис."
                )
                return
            await self._send_view(
                msg.chat.id, self.HELP_TEXT, reply_markup=ui.single_close_kb()
            )

        @dp.message(Command("menu"))
        async def cmd_menu(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._send_view(
                msg.chat.id,
                self._menu_text(msg.chat.id),
                reply_markup=ui.main_menu(self._target_label_for(msg.chat.id)),
            )

        @dp.message(Command("status"))
        async def cmd_status(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_status(msg)

        @dp.message(Command("health"))
        async def cmd_health(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_status(msg)

        @dp.message(Command("balance"))
        async def cmd_balance(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_balance(msg)

        @dp.message(Command("orders"))
        async def cmd_orders(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_orders(msg)

        @dp.message(Command("problems"))
        async def cmd_problems(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_problems(msg)

        @dp.message(Command("pending_confirm"))
        async def cmd_pending_confirm(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_pending_confirm(msg)

        @dp.message(Command("sync_pending_confirm"))
        async def cmd_sync_pending_confirm(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_sync_pending_confirm(msg)

        @dp.message(Command("stats"))
        async def cmd_stats(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_stats(msg)

        @dp.message(Command("sync"))
        async def cmd_sync(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_sync(msg)

        @dp.message(Command("lots"))
        async def cmd_lots(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_lots(msg)

        @dp.message(Command("mappings"))
        async def cmd_mappings(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_mappings(msg)

        @dp.message(Command("groups"))
        async def cmd_groups(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_groups(msg)

        @dp.message(Command("map"))
        async def cmd_map(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_map(msg)

        @dp.message(Command("unmap"))
        async def cmd_unmap(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_unmap(msg)

        @dp.message(Command("ns_search"))
        async def cmd_ns_search(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_ns_search(msg)

        @dp.message(Command("ns_cats"))
        async def cmd_ns_cats(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_ns_cats(msg)

        @dp.message(Command("funpay_reconnect"))
        async def cmd_funpay_reconnect(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_funpay_reconnect(msg)

        @dp.message(Command("calc"))
        async def cmd_calc(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_calc(msg)

        @dp.message(Command("inspect_lot"))
        async def cmd_inspect_lot(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_inspect_lot(msg)

        @dp.message(Command("lot_status"))
        async def cmd_lot_status(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_lot_status(msg)

        @dp.message(Command("setmarkup"))
        async def cmd_setmarkup(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_setmarkup(msg)

        @dp.message(Command("reset_markups"))
        async def cmd_reset_markups(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_reset_markups(msg)

        @dp.message(Command("setdefault"))
        async def cmd_setdefault(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_setdefault(msg)

        @dp.message(Command("settings"))
        async def cmd_settings(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_show_settings(msg)

        @dp.message(Command("force_sync"))
        async def cmd_force_sync(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_force_sync(msg)

        @dp.message(Command("funpay_check"))
        async def cmd_funpay_check(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._do_funpay_check(msg)

        @dp.message(Command("clear_target"))
        async def cmd_clear_target(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            self._clear_target(msg.chat.id)
            await msg.answer("Цель сброшена.", reply_markup=ui.single_close_kb())

        # Пользователь иногда пишет "setmarkup ..." без ведущего "/".
        # Telegram тогда не считает это командой, поэтому явно поддерживаем
        # такие сообщения для владельца, чтобы бот не выглядел "зависшим".
        @dp.message(F.text)
        async def cmd_plain_text_alias(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await self._dispatch_plain_text_command(msg)

        # ----- callback router -----

        @dp.callback_query(F.data == "noop")
        async def cb_noop(cq: CallbackQuery) -> None:
            await cq.answer()

        @dp.callback_query(F.data == "close")
        async def cb_close(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            if cq.message is not None:
                with suppress_telegram():
                    await cq.message.delete()
            await cq.answer()

        @dp.callback_query(F.data == "sync_pending_confirm")
        async def cb_sync_pending_confirm(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await cq.answer("Синхронизирую с FunPay...", show_alert=False)
            # Создаём искусственный «msg»-контекст: _do_sync_pending_confirm
            # читает только chat.id, поэтому достаточно cq.message.
            if cq.message is None:
                return
            await self._do_sync_pending_confirm(cq.message)

        @dp.callback_query(F.data == "target:clear")
        async def cb_target_clear(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            self._clear_target(cq.from_user.id)
            await cq.answer("Цель сброшена", show_alert=False)
            await self._edit_or_answer(
                cq, self._menu_text(cq.from_user.id),
                reply_markup=ui.main_menu(self._target_label_for(cq.from_user.id)),
            )

        @dp.callback_query(F.data.startswith("menu:"))
        async def cb_menu(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_menu_click(cq)

        @dp.callback_query(F.data.startswith("pg:"))
        async def cb_pg(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_page_click(cq)

        @dp.callback_query(F.data.startswith("act:"))
        async def cb_act(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_action_click(cq)

        @dp.callback_query(F.data.startswith("calc:"))
        async def cb_calc(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_calc_click(cq)

        @dp.callback_query(F.data.startswith("newlot:"))
        async def cb_newlot(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_newlot_click(cq)

        @dp.callback_query(F.data.startswith("mapconfirm:"))
        async def cb_mapconfirm(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_mapconfirm_click(cq)

        @dp.callback_query(F.data.startswith("settings:"))
        async def cb_settings(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_settings_click(cq)

        @dp.callback_query(F.data.startswith("group:"))
        async def cb_group(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_group_click(cq)

        # Кнопки на алертах про manual_hold: hold:retry/done/show:<funpay_order_id>.
        # Эти алерты приходят из processor._trigger_manual_hold и из reconciler
        # для застрявших заказов. callback_data не session-based — несёт прямой
        # funpay_order_id, чтобы alert не "протух" по rotation сессий.
        @dp.callback_query(F.data.startswith("hold:"))
        async def cb_hold(cq: CallbackQuery) -> None:
            if not self._is_owner(cq):
                await cq.answer()
                return
            await self._on_hold_click(cq)

    # ─────────────── общие хелперы ───────────────

    def _target_label_for(self, chat_id: int) -> str | None:
        lot_id = self._target_lots.get(chat_id)
        if lot_id is None:
            return None
        label = self._target_labels.get(chat_id)
        if label:
            return f"{label} (#{lot_id})"
        return f"#{lot_id}"

    def _clear_target(self, chat_id: int) -> None:
        self._target_lots.pop(chat_id, None)
        self._target_labels.pop(chat_id, None)

    @staticmethod
    def _extract_newlot_title_from_message_text(text: str | None, lot_id: int) -> str | None:
        """Достать название лота из Telegram-уведомления о новом лоте."""
        if not text:
            return None
        lines = [line.strip() for line in text.splitlines()]
        lot_id_text = str(lot_id)
        for idx, line in enumerate(lines):
            if lot_id_text not in line:
                continue
            for candidate in lines[idx + 1:]:
                if not candidate:
                    continue
                if candidate.startswith("Маппинга ") or candidate.startswith("Возможные "):
                    return None
                return candidate[:120]
        return None

    async def _known_lot_label(self, lot_id: int, cq: CallbackQuery | None = None) -> str:
        """Человекочитаемая подпись лота для target/risk-check."""
        try:
            async with session_factory()() as session:
                row = await session.get(KnownLot, lot_id)
                if row is not None and row.title:
                    return row.title[:120]
        except Exception:
            pass
        message_text = None
        if cq is not None and cq.message is not None:
            message_text = getattr(cq.message, "text", None)
            if not message_text:
                message_text = getattr(cq.message, "html_text", None)
        from_message = self._extract_newlot_title_from_message_text(message_text, lot_id)
        return from_message or f"#{lot_id}"

    def _menu_text(self, chat_id: int) -> str:
        target = self._target_label_for(chat_id)
        if target:
            return (
                ui.MENU_GREETING
                + f"\n\n🎯 <b>Целевой лот:</b> <code>{html.escape(target)}</code>\n"
                + "<i>Выбери услугу в Каталоге NS или Поиске — замапим в один клик.</i>"
            )
        return ui.MENU_GREETING

    async def _dispatch_plain_text_command(self, msg: Message) -> bool:
        """
        Поддержка команд без ведущего слеша: "setmarkup 69300023 5.5",
        "sync", "menu" и т.п. Возвращает True, если сообщение было командой.
        """
        text = (msg.text or "").strip()
        if not text or text.startswith("/"):
            return False
        cmd = text.split(maxsplit=1)[0].lower()
        aliases = {
            "menu": self._plain_menu,
            "меню": self._plain_menu,
            "sync": self._plain_sync,
            "синхронизация": self._plain_sync,
            "setmarkup": self._plain_setmarkup,
            "markup": self._plain_setmarkup,
            "наценка": self._plain_setmarkup,
            "settings": self._plain_settings,
            "настройки": self._plain_settings,
        }
        handler = aliases.get(cmd)
        if handler is None:
            return False
        await handler(msg, text)
        return True

    def _slash_msg(self, msg: Message, text: str):
        return SimpleNamespace(
            text="/" + text,
            answer=msg.answer,
            chat=msg.chat,
            from_user=msg.from_user,
        )

    async def _plain_menu(self, msg: Message, text: str) -> None:
        await self._send_view(
            msg.chat.id,
            self._menu_text(msg.chat.id),
            reply_markup=ui.main_menu(self._target_label_for(msg.chat.id)),
        )

    async def _plain_sync(self, msg: Message, text: str) -> None:
        await self._do_sync(self._slash_msg(msg, text))  # type: ignore[arg-type]

    async def _plain_setmarkup(self, msg: Message, text: str) -> None:
        await self._do_setmarkup(self._slash_msg(msg, text))  # type: ignore[arg-type]

    async def _plain_settings(self, msg: Message, text: str) -> None:
        await self._do_show_settings(self._slash_msg(msg, text))  # type: ignore[arg-type]

    async def _get_stock(self, *, force: bool = False) -> StockResponse:
        """NS-каталог с кэшем 60 секунд."""
        async with self._stock_lock:
            now = time.time()
            if not force and self._stock_cache is not None:
                ts, cached = self._stock_cache
                if now - ts < 60.0:
                    return cached
            async with NSClient() as ns:
                stock = await asyncio.wait_for(ns.get_stock(), timeout=NS_TIMEOUT_SECONDS)
            self._stock_cache = (now, stock)
            return stock

    async def _safe_ns_balance(self) -> str:
        try:
            async with NSClient() as ns:
                bal = await asyncio.wait_for(
                    ns.check_balance(), timeout=NS_TIMEOUT_SECONDS
                )
            return f"{bal.balance}"
        except asyncio.TimeoutError:
            return "<i>timeout</i>"
        except Exception as exc:
            return f"<i>n/a ({html.escape(str(exc))[:80]})</i>"

    async def _safe_fp_status(self) -> str:
        if self._funpay_client is None:
            return "не подключён"
        try:
            return (
                f"id={self._funpay_client.account_id}, "
                f"username={self._funpay_client.username}"
            )
        except Exception as exc:
            return f"<i>ошибка ({html.escape(str(exc))[:80]})</i>"

    async def _edit_or_answer(
        self,
        cq: CallbackQuery,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """
        Редактируем сообщение, которое тапнул пользователь. Если редакт не
        получился — оставляем пользователю alert, не плодим новые сообщения.
        Так история чата не зарастает дубликатами «Маппинги / Всего: 1».
        """
        if cq.message is None:
            await cq.answer(
                "Это старое сообщение, кнопки уже неактивны. Открой /menu заново.",
                show_alert=True,
            )
            return
        try:
            await cq.message.edit_text(text, reply_markup=reply_markup)
            # Этот message теперь — единственная «панель управления» в чате
            chat_id = cq.message.chat.id
            self._control_msg[chat_id] = cq.message.message_id
        except TelegramBadRequest as exc:
            err = str(exc).lower()
            if "message is not modified" in err:
                return
            logger.debug(f"edit_text упал ({err[:80]}); прошу открыть /menu заново")
            await cq.answer(
                "Не могу обновить старое сообщение. Открой /menu заново.",
                show_alert=True,
            )

    async def _send_view(
        self,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        """
        Показывает «панель» в чате. Всегда отправляет новое сообщение внизу,
        предварительно удаляя предыдущую панель (если она ещё свежая).

        Зачем именно так:
        - попытка edit_message_text молча правит сообщение далеко вверху
          истории, и юзеру кажется, что бот не отвечает на команду;
        - delete+send гарантирует визуальный отклик на каждый /menu, /orders
          и т.п., а отсутствие каскада панелей обеспечивается удалением старой.
        """
        if self._bot is None:
            return None
        prev_id = self._control_msg.pop(chat_id, None)
        if prev_id is not None:
            try:
                await self._bot.delete_message(chat_id, prev_id)
            except TelegramBadRequest:
                pass
            except Exception as exc:
                logger.debug(f"delete prev control: {exc}")
        new_msg = await self._bot.send_message(
            chat_id, text, reply_markup=reply_markup
        )
        self._control_msg[chat_id] = new_msg.message_id
        return new_msg

    # ─────────────── /start ───────────────

    @_guard
    async def _on_start(self, msg: Message) -> None:
        owner = self._settings.telegram_chat_id
        if owner is None:
            await msg.answer(
                f"Привет! Твой <code>chat_id = {msg.chat.id}</code>.\n\n"
                f"Вставь его в <code>.env</code>:\n"
                f"<pre>TELEGRAM_CHAT_ID={msg.chat.id}</pre>\n\n"
                f"После перезапуска бот будет принимать команды только от тебя."
            )
            return
        if msg.chat.id != owner:
            await msg.answer("Этот бот — личный, чужие команды я не выполняю.")
            return
        await self._send_view(
            msg.chat.id,
            self._menu_text(msg.chat.id),
            reply_markup=ui.main_menu(self._target_label_for(msg.chat.id)),
        )

    # ─────────────── меню (callback) ───────────────

    @_guard
    async def _on_menu_click(self, cq: CallbackQuery) -> None:
        action = (cq.data or "").split(":", 1)[1] if ":" in (cq.data or "") else ""
        await cq.answer()
        if action == "home":
            await self._edit_or_answer(
                cq,
                self._menu_text(cq.from_user.id),
                reply_markup=ui.main_menu(self._target_label_for(cq.from_user.id)),
            )
        elif action == ui.MENU_KIND_STATUS:
            await self._show_status_via_cq(cq)
        elif action == ui.MENU_KIND_BALANCE:
            await self._show_balance_via_cq(cq)
        elif action == ui.MENU_KIND_LOTS:
            await self._show_lots_via_cq(cq)
        elif action == ui.MENU_KIND_MAPS:
            await self._show_mappings_via_cq(cq)
        elif action == ui.MENU_KIND_GROUPS:
            await self._show_groups_via_cq(cq)
        elif action == ui.MENU_KIND_NS_CATS:
            await self._show_ns_cats_via_cq(cq)
        elif action == ui.MENU_KIND_NS_SEARCH:
            text = ui.HINT_NS_SEARCH
            target = self._target_label_for(cq.from_user.id)
            if target:
                text += (
                    f"\n\n🎯 <b>Целевой лот:</b> <code>{html.escape(target)}</code>"
                )
            await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())
        elif action == ui.MENU_KIND_ORDERS:
            await self._show_orders_via_cq(cq)
        elif action == ui.MENU_KIND_PROBLEMS:
            await self._show_problems_via_cq(cq)
        elif action == ui.MENU_KIND_PENDING:
            await self._show_pending_confirm_via_cq(cq)
        elif action == ui.MENU_KIND_STATS:
            await self._show_stats_via_cq(cq)
        elif action == ui.MENU_KIND_SYNC:
            await self._run_sync_via_cq(cq)
        elif action == ui.MENU_KIND_RECONNECT:
            await self._run_reconnect_via_cq(cq)
        elif action == ui.MENU_KIND_HELP:
            await self._edit_or_answer(cq, self.HELP_TEXT, reply_markup=ui.single_close_kb())
        else:
            await cq.answer(f"Неизвестная команда меню: {action}")

    # ─────────────── пагинация (callback) ───────────────

    @_guard
    async def _on_page_click(self, cq: CallbackQuery) -> None:
        # pg:<kind>:<sid>:<page>
        parts = (cq.data or "").split(":")
        if len(parts) != 4:
            await cq.answer("Некорректный callback")
            return
        _, kind, sid, page_str = parts
        try:
            page = int(page_str)
        except ValueError:
            await cq.answer("Некорректная страница")
            return
        await cq.answer()
        await self._render_paginated(cq, kind=kind, sid=sid, page=page)

    # ─────────────── действия (callback) ───────────────

    @_guard
    async def _on_action_click(self, cq: CallbackQuery) -> None:
        # act:<kind>:<sid>:<idx>
        parts = (cq.data or "").split(":")
        if len(parts) != 4:
            await cq.answer("Некорректное действие")
            return
        _, kind, sid, idx_str = parts
        try:
            idx = int(idx_str)
        except ValueError:
            await cq.answer("Некорректный индекс")
            return

        sess = self._sessions.get(sid)
        if sess is None:
            await cq.answer("Сессия устарела, открой список заново", show_alert=True)
            return
        if idx < 0 or idx >= len(sess.items):
            await cq.answer("Элемент не найден")
            return

        item = sess.items[idx]

        if kind == "fp_target":
            await self._act_fp_target(cq, item)
        elif kind == "ns_map":
            await self._act_ns_map(cq, item)
        elif kind == "cat_open":
            await self._act_cat_open(cq, item)
        elif kind == "lot_calc":
            await self._act_lot_calc(cq, item)
        elif kind == "lot_inspect":
            await self._act_lot_inspect(cq, item)
        elif kind == "lot_card":
            await self._act_lot_card(cq, item)
        elif kind == "map_toggle":
            await self._act_map_toggle(cq, item)
        elif kind == "map_delete":
            await self._act_map_delete(cq, item)
        elif kind == "group_open":
            await self._act_group_open(cq, item)
        elif kind == "map_group":
            await self._act_map_group(cq, item)
        elif kind == "group_assign":
            await self._act_group_assign(cq, item)
        elif kind == "order_retry":
            await self._act_order_retry(cq, item)
        elif kind == "order_manual_done":
            await self._act_order_manual_done(cq, item)
        elif kind == "problem_force_sync":
            await self._act_problem_force_sync(cq, item)
        elif kind == "problem_enable_mapping":
            await self._act_problem_enable_mapping(cq, item)
        else:
            await cq.answer(f"Неизвестное действие: {kind}")

    # ─────────────── рендеринг страниц ───────────────

    async def _render_paginated(
        self,
        cq: CallbackQuery,
        *,
        kind: str,
        sid: str,
        page: int,
    ) -> None:
        sess = self._sessions.get(sid)
        if sess is None:
            await self._edit_or_answer(
                cq,
                "⌛ Сессия устарела. Открой список заново.",
                reply_markup=ui.single_close_kb(),
            )
            return

        page_items, page, total_pages = paginate(sess.items, page)

        if kind == "ns_search":
            text, kb = self._build_ns_search_page(sess, sid, page_items, page, total_pages)
        elif kind == "ns_cats":
            text, kb = self._build_ns_cats_page(sess, sid, page_items, page, total_pages)
        elif kind == "ns_cat_services":
            text, kb = self._build_ns_cat_services_page(
                sess, sid, page_items, page, total_pages
            )
        elif kind == "lots":
            text, kb = self._build_lots_page(sess, sid, page_items, page, total_pages)
        elif kind == "mappings":
            text, kb = self._build_mappings_page(sess, sid, page_items, page, total_pages)
        elif kind == "groups":
            text, kb = self._build_groups_page(sess, sid, page_items, page, total_pages)
        elif kind == "group_mappings":
            text, kb = self._build_group_mappings_page(
                sess, sid, page_items, page, total_pages
            )
        elif kind == "group_assign":
            text, kb = self._build_group_assign_page(
                sess, sid, page_items, page, total_pages
            )
        elif kind == "orders":
            text, kb = self._build_orders_page(sess, sid, page_items, page, total_pages)
        elif kind == "problems":
            text, kb = self._build_problems_page(sess, sid, page_items, page, total_pages)
        else:
            await cq.answer(f"Неизвестный список: {kind}")
            return

        await self._edit_or_answer(cq, text, reply_markup=kb)

    # ----- builder'ы страниц -----

    def _target_hint(self, chat_id: int) -> str:
        """Подсказка для заголовка списков NS: на какой лот мапим."""
        lot_id = self._target_lots.get(chat_id)
        if lot_id is None:
            return (
                "<i>Выбери лот в «🛒 Лоты FunPay», чтобы мапить одним кликом</i>"
            )
        label = self._target_labels.get(chat_id) or f"#{lot_id}"
        return f"🎯 Цель: <b>{label}</b> (<code>{lot_id}</code>)"

    def _build_ns_search_page(self, sess, sid, page_items, page, total_pages):
        target = ""
        if sess.meta.get("chat_id") is not None:
            target = "\n" + self._target_hint(sess.meta["chat_id"]) + "\n"
        text = ui.render_list(
            page_items=page_items,
            formatter=ui.format_ns_service_line,
            title=f"🔍 NS-поиск: «{sess.title}»{target}",
            page=page,
            total_pages=total_pages,
            total_items=len(sess.items),
        )
        from aiogram.types import InlineKeyboardButton

        item_buttons: list[list[InlineKeyboardButton]] = []
        for local_idx, svc in enumerate(page_items):
            global_idx = page * PAGE_SIZE + local_idx
            item_buttons.append([
                InlineKeyboardButton(
                    text=f"✅ {ui.ns_service_label(svc)}",
                    callback_data=f"act:ns_map:{sid}:{global_idx}",
                )
            ])
        kb = ui.list_keyboard(
            kind="ns_search",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=item_buttons,
        )
        return text, kb

    def _build_ns_cats_page(self, sess, sid, page_items, page, total_pages):
        target = ""
        if sess.meta.get("chat_id") is not None:
            target = "\n" + self._target_hint(sess.meta["chat_id"]) + "\n"
        text = ui.render_list(
            page_items=page_items,
            formatter=ui.format_ns_category_line,
            title=f"🗂 Категории NS{target}",
            page=page,
            total_pages=total_pages,
            total_items=len(sess.items),
        )
        from aiogram.types import InlineKeyboardButton

        item_buttons: list[list[InlineKeyboardButton]] = []
        for local_idx, cat in enumerate(page_items):
            global_idx = page * PAGE_SIZE + local_idx
            name = ui.short_title(cat.category_name, limit=28)
            stock_total = sum(s.in_stock for s in cat.services)
            item_buttons.append([
                InlineKeyboardButton(
                    text=f"📂 {name} · {len(cat.services)} · stock {stock_total}",
                    callback_data=f"act:cat_open:{sid}:{global_idx}",
                )
            ])
        kb = ui.list_keyboard(
            kind="ns_cats",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=item_buttons,
        )
        return text, kb

    def _build_ns_cat_services_page(self, sess, sid, page_items, page, total_pages):
        cat_name = sess.meta.get("category_name", "—")
        target = ""
        if sess.meta.get("chat_id") is not None:
            target = "\n" + self._target_hint(sess.meta["chat_id"]) + "\n"
        text = ui.render_list(
            page_items=page_items,
            formatter=ui.format_ns_service_line,
            title=f"📂 {cat_name}{target}",
            page=page,
            total_pages=total_pages,
            total_items=len(sess.items),
        )
        from aiogram.types import InlineKeyboardButton

        item_buttons: list[list[InlineKeyboardButton]] = []
        for local_idx, svc in enumerate(page_items):
            global_idx = page * PAGE_SIZE + local_idx
            item_buttons.append([
                InlineKeyboardButton(
                    text=f"✅ {ui.ns_service_label(svc)}",
                    callback_data=f"act:ns_map:{sid}:{global_idx}",
                )
            ])
        extra_rows = [[
            InlineKeyboardButton(
                text="↩ К категориям",
                callback_data=f"menu:{ui.MENU_KIND_NS_CATS}",
            )
        ]]
        kb = ui.list_keyboard(
            kind="ns_cat_services",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=item_buttons,
            extra_rows=extra_rows,
        )
        return text, kb

    def _build_lots_page(self, sess, sid, page_items, page, total_pages):
        for local_idx, lot in enumerate(page_items):
            try:
                setattr(lot, "_ui_index", page * PAGE_SIZE + local_idx + 1)
            except Exception:
                pass
        text = ui.render_list(
            page_items=page_items,
            formatter=ui.format_funpay_lot_line,
            title="🛒 Лоты на FunPay",
            page=page,
            total_pages=total_pages,
            total_items=len(sess.items),
            empty_text="Лотов нет.",
        )
        from aiogram.types import InlineKeyboardButton

        item_buttons: list[list[InlineKeyboardButton]] = []
        for local_idx, lot in enumerate(page_items):
            global_idx = page * PAGE_SIZE + local_idx
            shown_idx = global_idx + 1
            row_top = [
                InlineKeyboardButton(
                    text=f"ℹ️ Открыть карточку #{shown_idx}",
                    callback_data=f"act:lot_card:{sid}:{global_idx}",
                ),
            ]
            row_bot = [
                InlineKeyboardButton(
                    text=f"📊 #{shown_idx}",
                    callback_data=f"act:lot_calc:{sid}:{global_idx}",
                ),
                InlineKeyboardButton(
                    text=f"🔬 #{shown_idx}",
                    callback_data=f"act:lot_inspect:{sid}:{global_idx}",
                ),
                InlineKeyboardButton(
                    text=f"🎯 #{shown_idx}",
                    callback_data=f"act:fp_target:{sid}:{global_idx}",
                ),
            ]
            item_buttons.append(row_top)
            item_buttons.append(row_bot)
        kb = ui.list_keyboard(
            kind="lots",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=item_buttons,
        )
        return text, kb

    def _build_mappings_page(self, sess, sid, page_items, page, total_pages):
        text = ui.render_list(
            page_items=page_items,
            formatter=ui.format_mapping_line,
            title="🗺 Маппинги",
            page=page,
            total_pages=total_pages,
            total_items=len(sess.items),
            empty_text=(
                "Маппингов нет. Открой «🛒 Лоты FunPay», нажми отдельную кнопку 🎯 Цель, "
                "потом из «🗂 Каталог NS» или /ns_search нажми «✅ …» на услуге."
            ),
        )
        from aiogram.types import InlineKeyboardButton

        item_buttons: list[list[InlineKeyboardButton]] = []
        for local_idx, m in enumerate(page_items):
            global_idx = page * PAGE_SIZE + local_idx
            toggle_emoji = "⏸ Выкл" if m.enabled else "▶ Вкл"
            label = ui.mapping_label(m, max_len=24)
            row = [
                InlineKeyboardButton(
                    text=f"{toggle_emoji} · {label}",
                    callback_data=f"act:map_toggle:{sid}:{global_idx}",
                ),
                InlineKeyboardButton(
                    text="📁",
                    callback_data=f"act:map_group:{sid}:{global_idx}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"act:map_delete:{sid}:{global_idx}",
                ),
            ]
            item_buttons.append(row)
        kb = ui.list_keyboard(
            kind="mappings",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=item_buttons,
        )
        return text, kb

    def _build_groups_page(self, sess, sid, page_items, page, total_pages):
        text = ui.render_list(
            page_items=page_items,
            formatter=ui.format_lot_group_line,
            title="📁 Группы лотов",
            page=page,
            total_pages=total_pages,
            total_items=len(sess.items),
            empty_text="Группы ещё не созданы.",
        )
        from aiogram.types import InlineKeyboardButton

        item_buttons: list[list[InlineKeyboardButton]] = []
        for local_idx, group in enumerate(page_items):
            global_idx = page * PAGE_SIZE + local_idx
            item_buttons.append([
                InlineKeyboardButton(
                    text=f"📁 {ui.short_title(group.name, 28)}",
                    callback_data=f"act:group_open:{sid}:{global_idx}",
                ),
                InlineKeyboardButton(
                    text="👁 Preview",
                    callback_data=f"group:preview:{group.id}",
                ),
                InlineKeyboardButton(
                    text="−1%",
                    callback_data=f"group:markup_delta:{group.id}:-1",
                ),
                InlineKeyboardButton(
                    text="+1%",
                    callback_data=f"group:markup_delta:{group.id}:1",
                ),
            ])
            item_buttons.append([
                InlineKeyboardButton(
                    text="5%",
                    callback_data=f"group:markup_set:{group.id}:5",
                ),
                InlineKeyboardButton(
                    text="6%",
                    callback_data=f"group:markup_set:{group.id}:6",
                ),
                InlineKeyboardButton(
                    text="12.5%",
                    callback_data=f"group:markup_set:{group.id}:12.5",
                ),
                InlineKeyboardButton(
                    text="default",
                    callback_data=f"group:markup_default:{group.id}",
                ),
            ])
        kb = ui.list_keyboard(
            kind="groups",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=item_buttons,
        )
        return text, kb

    def _build_group_mappings_page(self, sess, sid, page_items, page, total_pages):
        group_name = sess.meta.get("group_name", "Группа")
        text = ui.render_list(
            page_items=page_items,
            formatter=ui.format_mapping_line,
            title=f"📁 {group_name}",
            page=page,
            total_pages=total_pages,
            total_items=len(sess.items),
            empty_text="В этой группе пока нет маппингов.",
        )
        from aiogram.types import InlineKeyboardButton

        extra_rows = [[
            InlineKeyboardButton(
                text="↩ К группам",
                callback_data=f"menu:{ui.MENU_KIND_GROUPS}",
            )
        ]]
        kb = ui.list_keyboard(
            kind="group_mappings",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=[],
            extra_rows=extra_rows,
        )
        return text, kb

    def _build_group_assign_page(self, sess, sid, page_items, page, total_pages):
        mapping_id = sess.meta.get("mapping_id")
        label = sess.meta.get("mapping_label", "mapping")
        text = ui.render_list(
            page_items=page_items,
            formatter=ui.format_lot_group_line,
            title=f"📁 Выбор группы для {label}",
            page=page,
            total_pages=total_pages,
            total_items=len(sess.items),
            empty_text="Группы ещё не созданы.",
        )
        from aiogram.types import InlineKeyboardButton

        item_buttons: list[list[InlineKeyboardButton]] = []
        for local_idx, group in enumerate(page_items):
            global_idx = page * PAGE_SIZE + local_idx
            item_buttons.append([
                InlineKeyboardButton(
                    text=f"✅ {ui.short_title(group.name, 32)}",
                    callback_data=f"act:group_assign:{sid}:{global_idx}",
                )
            ])
        extra_rows = [[
            InlineKeyboardButton(
                text="Без группы",
                callback_data=f"group:assign_none:{mapping_id}",
            )
        ]]
        kb = ui.list_keyboard(
            kind="group_assign",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=item_buttons,
            extra_rows=extra_rows,
        )
        return text, kb

    def _build_orders_page(self, sess, sid, page_items, page, total_pages):
        body = "\n".join(_format_order_line(o) for o in page_items) or "<i>пусто</i>"
        text = (
            f"📦 <b>Последние заказы</b>\n"
            f"Всего: <b>{len(sess.items)}</b> · "
            f"страница <b>{page + 1}/{total_pages or 1}</b>\n"
            "─" * 8
            + "\n"
            + body
        )
        kb = ui.list_keyboard(
            kind="orders",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=[],
        )
        return text, kb

    def _build_problems_page(self, sess, sid, page_items, page, total_pages):
        body = "\n\n".join(_format_problem_line(o) for o in page_items) or "<i>пусто</i>"
        text = (
            f"🧯 <b>Панель проблем</b>\n"
            f"Всего: <b>{len(sess.items)}</b> · "
            f"страница <b>{page + 1}/{total_pages or 1}</b>\n"
            "─" * 8
            + "\n"
            + body
        )
        from aiogram.types import InlineKeyboardButton

        item_buttons: list[list[InlineKeyboardButton]] = []
        for local_idx, order in enumerate(page_items):
            global_idx = page * PAGE_SIZE + local_idx
            row: list[InlineKeyboardButton] = []
            if order.status in ("pins_ready", "manual_hold"):
                row.append(
                    InlineKeyboardButton(
                        text=f"🔁 Retry #{order.funpay_order_id}",
                        callback_data=f"act:order_retry:{sid}:{global_idx}",
                    )
                )
            if order.status == "manual_hold":
                row.append(
                    InlineKeyboardButton(
                        text="✅ Уже выдал руками",
                        callback_data=f"act:order_manual_done:{sid}:{global_idx}",
                    )
                )
            row.append(
                InlineKeyboardButton(
                    text="🔄 Force sync lot",
                    callback_data=f"act:problem_force_sync:{sid}:{global_idx}",
                )
            )
            row.append(
                InlineKeyboardButton(
                    text="▶ Mapping",
                    callback_data=f"act:problem_enable_mapping:{sid}:{global_idx}",
                )
            )
            item_buttons.append(row)
        kb = ui.list_keyboard(
            kind="problems",
            sid=sid,
            page=page,
            total_pages=total_pages,
            item_buttons=item_buttons,
        )
        return text, kb

    # ─────────────── реализации команд (показ из cmd или из cq) ───────────────

    @_guard
    async def _do_status(self, msg: Message) -> None:
        text = await self._render_status_text()
        await self._send_view(msg.chat.id, text, reply_markup=ui.single_close_kb())

    @_guard
    async def _show_status_via_cq(self, cq: CallbackQuery) -> None:
        text = await self._render_status_text()
        await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())

    async def _render_status_text(self) -> str:
        async with session_factory()() as session:
            stmt = select(SyncRun).order_by(desc(SyncRun.started_at)).limit(1)
            last_run = (await session.execute(stmt)).scalar_one_or_none()
            active_orders = {
                status: int(count or 0)
                for status, count in (
                    await session.execute(
                        select(Order.status, func.count(Order.id))
                        .where(Order.status.in_(("received", "ns_created", "ns_paid", "pins_ready", "manual_hold")))
                        .group_by(Order.status)
                    )
                ).all()
            }
            disabled_mappings = int(
                (
                    await session.execute(
                        select(func.count(Mapping.id)).where(Mapping.enabled.is_(False))
                    )
                ).scalar_one()
                or 0
            )
        ns_bal = await self._safe_ns_balance()
        fp_status = await self._safe_fp_status()
        # эффективный курс с премией
        try:
            rate = await get_rate_breakdown(self._settings)
            cur = self._settings.funpay_currency.value
            if rate.has_premium:
                rate_line = (
                    f"💱 Курс USD→{cur}: <b>{rate.effective:.4f}</b> "
                    f"(биржа {rate.base:.4f} + {rate.premium_percent:.1f}%)"
                )
            elif self._settings.funpay_currency.value == "USD":
                rate_line = "💱 Курс: <b>USD к USD = 1.0</b>"
            else:
                rate_line = (
                    f"💱 Курс USD→{cur}: <b>{rate.effective:.4f}</b> "
                    f"({rate.source})"
                )
        except Exception as exc:
            rate_line = f"💱 Курс: <i>n/a ({html.escape(str(exc))[:60]})</i>"
        text = _format_status_text(
            self._settings, last_run, ns_bal, fp_status, rate_line
        )
        active_total = sum(active_orders.values())
        health_lines = [
            "",
            "🩺 <b>Операционный health</b>",
            f"  Reconciler: <b>{'on' if self._settings.order_reconcile_enabled else 'off'}</b> "
            f"каждые {self._settings.order_reconcile_interval_seconds}с",
            f"  Active orders: <b>{active_total}</b> "
            f"(created={active_orders.get('ns_created', 0)}, "
            f"paid={active_orders.get('ns_paid', 0)}, "
            f"pins_ready={active_orders.get('pins_ready', 0)}, "
            f"hold={active_orders.get('manual_hold', 0)})",
            f"  Disabled mappings: <b>{disabled_mappings}</b>",
            f"  Guardrails: margin ≥ <b>{self._settings.sync_min_margin_percent:.1f}%</b>, "
            f"max price jump <b>{self._settings.sync_max_price_change_percent:.0f}%</b>, "
            f"reserve stock <b>{'on' if self._settings.sync_reserve_pending_orders else 'off'}</b>",
        ]
        return text + "\n".join(health_lines)

    @_guard
    async def _do_balance(self, msg: Message) -> None:
        text = await self._render_balance_text()
        await self._send_view(msg.chat.id, text, reply_markup=ui.single_close_kb())

    @_guard
    async def _show_balance_via_cq(self, cq: CallbackQuery) -> None:
        text = await self._render_balance_text()
        await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())

    async def _render_balance_text(self) -> str:
        lines = ["💰 <b>Балансы</b>"]
        ns_text = await self._safe_ns_balance()
        lines.append(f"  NS: <b>{ns_text}</b>")

        if self._funpay_client is None:
            lines.append("  FunPay: <i>не подключён</i>")
        else:
            try:
                fp_bal = await asyncio.wait_for(
                    self._funpay_client.get_funpay_balance(),
                    timeout=FP_TIMEOUT_SECONDS,
                )
                if fp_bal.get("error"):
                    lines.append(
                        f"  FunPay: <i>ошибка ({html.escape(str(fp_bal['error']))[:80]})</i>"
                    )
                else:
                    val = fp_bal.get("rub") or fp_bal.get("total") or fp_bal.get("available")
                    if val is not None:
                        lines.append(f"  FunPay (RUB): <b>{val}</b>")
                    else:
                        # Если не вытянули конкретное число — показываем raw,
                        # чтобы было видно что вернул FunPay
                        raw = fp_bal.get("raw_repr")
                        if raw:
                            lines.append(
                                f"  FunPay: <i>не распарсил, raw:</i> "
                                f"<code>{html.escape(str(raw)[:120])}</code>"
                            )
                        else:
                            lines.append("  FunPay: <i>пусто</i>")
            except asyncio.TimeoutError:
                lines.append("  FunPay: <i>timeout {0:.0f}s</i>".format(FP_TIMEOUT_SECONDS))
            except Exception as exc:
                lines.append(f"  FunPay: <i>{html.escape(str(exc))[:120]}</i>")
        return "\n".join(lines)

    @_guard
    async def _do_orders(self, msg: Message) -> None:
        sid, total = await self._collect_orders()
        if total == 0:
            await self._send_view(
                msg.chat.id, "Заказов ещё нет.",
                reply_markup=ui.single_close_kb(),
            )
            return
        await self._render_paginated_from_cmd(msg, kind="orders", sid=sid, page=0)

    @_guard
    async def _do_problems(self, msg: Message) -> None:
        sid, total = await self._collect_problems()
        if total == 0:
            await self._send_view(
                msg.chat.id,
                "🧯 Проблем нет: failed/pins_ready/manual_hold заказов и выключенных mappings не найдено.",
                reply_markup=ui.single_close_kb(),
            )
            return
        await self._render_paginated_from_cmd(msg, kind="problems", sid=sid, page=0)

    @_guard
    async def _do_pending_confirm(self, msg: Message) -> None:
        """
        /pending_confirm [hours=24] — заказы status=delivered, прошло >Nh,
        покупатель не нажал «подтвердить» (и саппорт тоже). С чанкингом
        под Telegram limit 4096 chars (на больших списках).
        """
        hours = _parse_hours_arg(msg.text, default=24)
        await self._render_pending_confirm(chat_id=msg.chat.id, hours=hours)

    @_guard
    async def _show_pending_confirm_via_cq(self, cq: CallbackQuery) -> None:
        await self._render_pending_confirm(chat_id=cq.from_user.id, hours=24)

    async def _render_pending_confirm(self, *, chat_id: int, hours: int) -> None:
        orders = await self._collect_pending_confirm(hours=hours)

        if not orders:
            await self._send_view(
                chat_id,
                f"✅ Нет заказов, ожидающих подтверждения дольше {hours}ч.",
                reply_markup=ui.single_close_kb(),
            )
            return

        # Сборка человекочитаемого списка с разбиением на чанки.
        # Telegram hard limit = 4096 chars; берём запас и режем по ~3500.
        header = (
            f"📋 Заказы старше {hours}ч без подтверждения "
            f"({len(orders)} шт.)\n"
            f"Готовый список для саппорта FunPay ниже.\n"
            f"⚠ Если в списке есть заказы, которые саппорт FunPay уже "
            f"подтвердил (тихо, без системки в чат) — нажми «🔄 Sync с "
            f"FunPay» под последним сообщением. Это вычистит фантомы.\n"
        )
        now = datetime.utcnow()
        item_lines = [
            f"• #{o.funpay_order_id} — {o.buyer_username or '—'}, "
            f"выдан {int((now - o.updated_at).total_seconds() // 3600)}ч назад"
            for o in orders
        ]

        # Чанк 1+: разбиваем item_lines по ~3500 chars,
        # последний чанк (без footer) + ВСЕГДА отдельным сообщением copy-block.
        chunks = _split_lines_to_chunks(
            header_lines=[header],
            body_lines=item_lines,
            max_chars=3500,
        )

        # Шлём все чанки списка. Кнопку закрытия — только на последний.
        for idx, text in enumerate(chunks):
            is_last_text_chunk = idx == len(chunks) - 1
            reply_markup = ui.single_close_kb() if is_last_text_chunk else None
            await self._send_view(chat_id, text, reply_markup=reply_markup)

        # Copy-block: одним или несколькими отдельными сообщениями,
        # без markdown-обёртки (раньше делали `...` — но если в чанке
        # внутри случится backtick, parse_mode сломается; чистый
        # plaintext всегда копируется через long-tap в Telegram).
        copy_chunks = _split_ids_to_copy_chunks(
            [o.funpay_order_id for o in orders],
            max_chars=3500,
        )
        for idx, ids_text in enumerate(copy_chunks):
            label = (
                "📥 Скопировать в саппорт (long-tap → copy):"
                if idx == 0
                else f"📥 …продолжение ({idx + 1}/{len(copy_chunks)}):"
            )
            await self._send_view(
                chat_id,
                f"{label}\n{ids_text}",
                reply_markup=(
                    ui.pending_confirm_kb()
                    if idx == len(copy_chunks) - 1
                    else None
                ),
            )

    async def _collect_pending_confirm(self, *, hours: int) -> list[Order]:
        async with session_factory()() as session:
            return await list_pending_confirmation(
                session, older_than_hours=hours, limit=500
            )

    @_guard
    async def _do_sync_pending_confirm(self, msg: Message) -> None:
        """
        /sync_pending_confirm — синхронизировать БД с реальным «Оплачен»
        на FunPay. FunPay-саппорт подтверждает заказы по нашему запросу
        тихо, без системного сообщения в чат, поэтому в БД они остаются
        delivered+NULL. Эта команда чистит фантомы.
        """
        chat_id = msg.chat.id
        if self._funpay_client is None:
            await self._send_view(
                chat_id,
                "⚠ FunPay-клиент сейчас не подключён. Сначала "
                "/funpay_reconnect, потом повтори команду.",
                reply_markup=ui.single_close_kb(),
            )
            return
        await self._send_view(
            chat_id, "🔄 Синхронизирую с FunPay...", reply_markup=None
        )
        try:
            stats = await sync_pending_confirmation(
                funpay_client=self._funpay_client,
                session_factory=session_factory,
            )
        except Exception as exc:
            logger.opt(exception=exc).warning(
                f"sync_pending_confirmation упал: {type(exc).__name__}: {exc}"
            )
            await self._send_view(
                chat_id,
                f"❌ Sync упал: <code>{type(exc).__name__}: {exc}</code>\n"
                "Подробности в логах сервиса.",
                reply_markup=ui.single_close_kb(),
            )
            return

        snapshot_complete = stats.get("snapshot_complete", True)
        warning_line = ""
        if not snapshot_complete:
            # Аудит #7: при неполном snapshot мы НЕ помечаем confirmed,
            # чтобы не закрыть реально оплачённые заказы.
            warning_line = (
                "\n⚠️ <b>Snapshot FunPay НЕПОЛНЫЙ</b> "
                f"(<code>{stats.get('truncated_reason', '?')}</code>) — "
                "автоматическое подтверждение пропущено, чтобы не закрыть "
                "реально оплачённые заказы. Запусти Sync ещё раз позже."
            )
        text = (
            "✅ <b>Sync /pending_confirm с FunPay</b>\n"
            f"• Сейчас «Оплачен» на FunPay: <b>{stats['paid_on_funpay']}</b>\n"
            f"• В БД было delivered+NULL: <b>{stats['delivered_unconfirmed_in_db']}</b>\n"
            f"• Помечено confirmed (закрыты тихо саппортом): <b>{stats['marked_confirmed']}</b>"
            f"{warning_line}\n\n"
            "Запусти /pending_confirm — список теперь должен соответствовать "
            "тем заказам, которые реально ждут подтверждения."
        )
        await self._send_view(chat_id, text, reply_markup=ui.single_close_kb())

    @_guard
    async def _do_stats(self, msg: Message) -> None:
        text = await self._render_profit_stats()
        await self._send_view(msg.chat.id, text, reply_markup=ui.single_close_kb())

    @_guard
    async def _show_orders_via_cq(self, cq: CallbackQuery) -> None:
        sid, total = await self._collect_orders()
        if total == 0:
            await self._edit_or_answer(
                cq, "Заказов ещё нет.", reply_markup=ui.single_close_kb()
            )
            return
        await self._render_paginated(cq, kind="orders", sid=sid, page=0)

    @_guard
    async def _show_problems_via_cq(self, cq: CallbackQuery) -> None:
        sid, total = await self._collect_problems()
        if total == 0:
            await self._edit_or_answer(
                cq,
                "🧯 Проблем нет: failed/pins_ready/manual_hold заказов и выключенных mappings не найдено.",
                reply_markup=ui.single_close_kb(),
            )
            return
        await self._render_paginated(cq, kind="problems", sid=sid, page=0)

    @_guard
    async def _show_stats_via_cq(self, cq: CallbackQuery) -> None:
        text = await self._render_profit_stats()
        await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())

    async def _collect_orders(self) -> tuple[str, int]:
        async with session_factory()() as session:
            stmt = select(Order).order_by(desc(Order.created_at)).limit(50)
            orders = list((await session.execute(stmt)).scalars().all())
        sid = self._sessions.put(orders, title="orders")
        return sid, len(orders)

    async def _collect_problems(self) -> tuple[str, int]:
        async with session_factory()() as session:
            stmt = (
                select(Order)
                .where(Order.status.in_(("failed", "pins_ready", "manual_hold")))
                .order_by(desc(Order.updated_at))
                .limit(50)
            )
            items = list((await session.execute(stmt)).scalars().all())
            disabled = list(
                (
                    await session.execute(
                        select(Mapping)
                        .where(Mapping.enabled.is_(False))
                        .order_by(Mapping.funpay_lot_id)
                        .limit(50)
                    )
                ).scalars().all()
            )
            for mapping in disabled:
                items.append(
                    SimpleNamespace(
                        funpay_order_id=f"mapping:{mapping.funpay_lot_id}",
                        funpay_lot_id=mapping.funpay_lot_id,
                        ns_service_id=mapping.ns_service_id,
                        ns_custom_id=None,
                        status="mapping_disabled",
                        error=f"Mapping выключен: {mapping.label or 'без label'}",
                        created_at=mapping.created_at,
                        updated_at=mapping.updated_at,
                    )
                )
        sid = self._sessions.put(items, title="problems")
        return sid, len(items)

    async def _render_profit_stats(self) -> str:
        rate = await get_rate_breakdown(self._settings)
        since = datetime.utcnow() - timedelta(days=7)
        async with session_factory()() as session:
            orders = list(
                (
                    await session.execute(
                        select(Order)
                        .where(Order.status == "delivered")
                        .where(Order.created_at >= since)
                        .order_by(desc(Order.created_at))
                    )
                ).scalars().all()
            )
            mappings = {
                m.funpay_lot_id: m
                for m in (await session.execute(select(Mapping))).scalars().all()
            }
            groups = {
                g.id: g.name
                for g in (await session.execute(select(LotGroup))).scalars().all()
            }

        revenue = cost = profit = withdrawal_fee = 0.0
        counted = 0
        by_group: dict[str, list[float]] = {}
        exact_count = 0
        for order in orders:
            fx = getattr(order, "fx_rate_at_sale", None) or rate.effective
            estimated = estimate_profit_rub(
                order.funpay_price_rub,
                order.ns_price_usd,
                fx,
                withdrawal_fee_percent=self._settings.funpay_withdrawal_fee_percent,
            )
            if estimated is None:
                continue
            order_revenue, order_cost, order_profit, _ = estimated
            if getattr(order, "fx_rate_at_sale", None) is not None:
                exact_count += 1
            revenue += order_revenue
            cost += order_cost
            withdrawal_fee += (
                order_revenue * self._settings.funpay_withdrawal_fee_percent / 100.0
            )
            profit += order_profit
            counted += 1
            mapping = mappings.get(order.funpay_lot_id)
            group_name = "Без группы"
            if mapping is not None and mapping.group_id is not None:
                group_name = groups.get(mapping.group_id, group_name)
            bucket = by_group.setdefault(group_name, [0.0, 0.0, 0.0])
            bucket[0] += 1
            bucket[1] += order_revenue
            bucket[2] += order_profit

        margin = profit / revenue * 100.0 if revenue > 0 else 0.0
        lines = [
            "📈 <b>Прибыль за 7 дней</b>",
            "",
            f"Заказов учтено: <b>{counted}</b>",
            f"Продажи: <b>{revenue:.0f} ₽</b>",
            f"Себестоимость NS: <b>{cost:.0f} ₽</b>",
            f"Вывод FunPay {self._settings.funpay_withdrawal_fee_percent:.1f}%: "
            f"<b>-{withdrawal_fee:.0f} ₽</b>",
            f"Чистая прибыль: <b>{profit:.0f} ₽</b>",
            f"Маржа: <b>{margin:.1f}%</b>",
            f"Точный курс в заказах: <b>{exact_count}/{counted}</b>",
            f"Курс для старых заказов: <b>{rate.effective:.4f}</b>",
        ]
        if by_group:
            lines.append("")
            lines.append("<b>По группам:</b>")
            for name, values in sorted(
                by_group.items(), key=lambda item: item[1][2], reverse=True
            )[:8]:
                count, group_revenue, group_profit = values
                group_margin = group_profit / group_revenue * 100.0 if group_revenue else 0.0
                lines.append(
                    f"• {html.escape(name)}: <b>{group_profit:.0f} ₽</b> "
                    f"({int(count)} шт, {group_margin:.1f}%)"
                )
        lines.append("")
        lines.append("<i>Новые заказы считаются по курсу на момент доставки; старые без сохранённого курса — по текущему курсу.</i>")
        return "\n".join(lines)

    async def _render_group_preview(self, group_id: int) -> str:
        if self._funpay_client is None:
            return "FunPay не подключён. Preview требует чтения текущих цен лотов."

        from src.config_runtime import get_global_markup_percent, get_stock_cap
        from src.sync.fx import get_usd_rub_rate
        from src.sync.stock_sync import _decide_for_one, _flatten_services

        async with session_factory()() as session:
            group = await session.get(LotGroup, group_id)
            if group is None:
                return "Группа не найдена."
            mappings = await list_mappings(session, only_enabled=True, group_id=group_id)
        if not mappings:
            return f"👁 <b>Preview группы {html.escape(group.name)}</b>\n\nАктивных маппингов нет."

        stock = await self._get_stock()
        services = _flatten_services(stock)
        fx_rate = await get_usd_rub_rate(self._settings)
        eff_markup = await get_global_markup_percent(self._settings)
        eff_stock_cap = await get_stock_cap(self._settings)

        decisions = []
        for mapping in mappings:
            decision = await _decide_for_one(
                services.get(mapping.ns_service_id),
                mapping,
                self._settings,
                fx_rate,
                self._funpay_client,
                effective_markup=eff_markup,
                effective_stock_cap=eff_stock_cap,
                group=group,
            )
            if decision is not None:
                decisions.append(decision)

        price_changes = [d for d in decisions if d.will_update_price]
        stock_changes = [d for d in decisions if d.will_update_stock]
        skipped = [d for d in decisions if d.skip_reason]
        lines = [
            f"👁 <b>Preview группы {html.escape(group.name)}</b>",
            "",
            f"Активных лотов: <b>{len(mappings)}</b>",
            f"Цена изменится: <b>{len(price_changes)}</b>",
            f"Сток изменится: <b>{len(stock_changes)}</b>",
            f"Skip: <b>{len(skipped)}</b>",
        ]
        if price_changes:
            lines.append("")
            lines.append("<b>Топ изменений цены:</b>")
            for decision in price_changes[:8]:
                label = html.escape(ui.short_title(decision.label, 36))
                current = "?" if decision.current_price is None else f"{decision.current_price:g}"
                target = f"{decision.target.round_price():g}"
                lines.append(f"• {label}: <b>{current}</b> → <b>{target}</b>")
        if skipped:
            lines.append("")
            lines.append("<b>Проблемы:</b>")
            for decision in skipped[:5]:
                label = html.escape(ui.short_title(decision.label, 30))
                lines.append(f"• {label}: <code>{html.escape(decision.skip_reason or '')[:90]}</code>")
        lines.append("")
        lines.append("Нажми 🔄 Синхронизация, чтобы применить эти изменения на FunPay.")
        return "\n".join(lines)

    async def _render_lot_card(self, funpay_lot_id: int, *, lot=None) -> str:
        title = (
            getattr(lot, "description", None)
            or getattr(lot, "title", None)
            or getattr(lot, "name", None)
            or ""
        )
        price = getattr(lot, "price", None) or getattr(lot, "cost", None) or "—"
        async with session_factory()() as session:
            mapping = (
                await session.execute(
                    select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
                )
            ).scalar_one_or_none()
            group = (
                await session.get(LotGroup, mapping.group_id)
                if mapping is not None and mapping.group_id is not None
                else None
            )
            orders = list(
                (
                    await session.execute(
                        select(Order)
                        .where(Order.funpay_lot_id == funpay_lot_id)
                        .order_by(desc(Order.created_at))
                        .limit(5)
                    )
                ).scalars().all()
            )

        lines = [
            f"🧾 <b>Карточка лота</b> <code>{funpay_lot_id}</code>",
            "",
            f"<b>Название:</b>\n{html.escape(title or '—')}",
            f"<b>Цена FunPay:</b> {html.escape(str(price))}",
        ]
        if mapping is None:
            lines.append("\n<b>Маппинг:</b> <i>не настроен</i>")
        else:
            markup = (
                f"{format_percent(mapping.markup_percent)}%"
                if mapping.markup_percent is not None else "default"
            )
            lines.extend([
                "",
                "<b>Маппинг:</b>",
                f"NS service: <code>{mapping.ns_service_id}</code>",
                f"Label: {html.escape(mapping.label or '—')}",
                f"Группа: {html.escape(group.name) if group is not None else '—'}",
                f"Markup: <b>{markup}</b>",
                f"Status: {'✅ enabled' if mapping.enabled else '⏸ disabled'}",
            ])
        if orders:
            lines.append("")
            lines.append("<b>Последние заказы:</b>")
            for order in orders:
                created = _to_moscow(order.created_at)
                when = "—" if created is None else created.strftime("%m-%d %H:%M")
                profit = (
                    f", profit {order.profit_rub:.0f} ₽"
                    if getattr(order, "profit_rub", None) is not None
                    else ""
                )
                lines.append(
                    f"• <code>{when}</code> #{html.escape(order.funpay_order_id)} "
                    f"{html.escape(order.status)}{profit}"
                )
        return "\n".join(lines)

    @_guard
    async def _do_sync(self, msg: Message) -> None:
        if self._sync_trigger is None:
            await msg.answer("Sync-движок не подключён к боту.")
            return
        progress = await msg.answer("⏳ Запускаю sync...")
        try:
            result = await self._sync_trigger()
            await progress.edit_text(
                f"✅ Готово: checked={result.get('checked', 0)}, "
                f"updated={result.get('updated', 0)}, "
                f"skipped={result.get('skipped', 0)}"
            )
        except Exception as exc:
            logger.exception("Sync trigger failed")
            await progress.edit_text(f"❌ Sync упал: <code>{html.escape(str(exc))}</code>")

    @_guard
    async def _run_sync_via_cq(self, cq: CallbackQuery) -> None:
        if self._sync_trigger is None:
            await cq.answer("Sync-движок не подключён", show_alert=True)
            return
        await self._edit_or_answer(cq, "⏳ Запускаю sync...", reply_markup=None)
        try:
            result = await self._sync_trigger()
            real = self._settings.enable_real_actions
            mode_line = (
                "🟢 Режим: <b>REAL</b> (изменения уходят на FunPay)"
                if real
                else "🟡 Режим: <b>DRY-RUN</b> "
                "(<code>ENABLE_REAL_ACTIONS=false</code>) — "
                "цены НЕ меняются на FunPay"
            )
            text = (
                f"✅ <b>Sync завершён</b>\n"
                f"{mode_line}\n\n"
                f"  checked: <b>{result.get('checked', 0)}</b>\n"
                f"  updated: <b>{result.get('updated', 0)}</b>\n"
                f"  skipped: <b>{result.get('skipped', 0)}</b>"
            )
            if not real:
                text += (
                    "\n\nЧтобы цены реально обновлялись:\n"
                    "1) в <code>.env</code> поставь "
                    "<code>ENABLE_REAL_ACTIONS=true</code>\n"
                    "2) <code>systemctl restart funpay-ns-bot</code>"
                )
            await self._edit_or_answer(
                cq, text, reply_markup=ui.single_close_kb(),
            )
        except Exception as exc:
            await self._edit_or_answer(
                cq,
                f"❌ Sync упал: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )

    @_guard
    async def _do_lots(self, msg: Message) -> None:
        if self._funpay_client is None:
            await self._send_view(
                msg.chat.id,
                "FunPay не подключён. Попробуй /funpay_reconnect.",
                reply_markup=ui.single_close_kb(),
            )
            return
        try:
            lots = await asyncio.wait_for(
                self._funpay_client.get_my_lots(), timeout=FP_TIMEOUT_SECONDS
            )
        except Exception as exc:
            await self._send_view(
                msg.chat.id,
                f"FunPay get_my_lots упал: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        if not lots:
            await self._send_view(
                msg.chat.id, "Лотов нет.", reply_markup=ui.single_close_kb()
            )
            return
        sid = self._sessions.put(lots, title="funpay lots")
        await self._render_paginated_from_cmd(msg, kind="lots", sid=sid, page=0)

    @_guard
    async def _show_lots_via_cq(self, cq: CallbackQuery) -> None:
        if self._funpay_client is None:
            await self._edit_or_answer(
                cq,
                "FunPay не подключён. Попробуй /funpay_reconnect.",
                reply_markup=ui.single_close_kb(),
            )
            return
        try:
            lots = await asyncio.wait_for(
                self._funpay_client.get_my_lots(), timeout=FP_TIMEOUT_SECONDS
            )
        except Exception as exc:
            await self._edit_or_answer(
                cq,
                f"FunPay get_my_lots упал: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        if not lots:
            await self._edit_or_answer(
                cq, "Лотов нет.", reply_markup=ui.single_close_kb()
            )
            return
        sid = self._sessions.put(lots, title="funpay lots")
        await self._render_paginated(cq, kind="lots", sid=sid, page=0)

    @_guard
    async def _do_mappings(self, msg: Message) -> None:
        sid, total = await self._collect_mappings()
        if total == 0:
            await self._send_view(
                msg.chat.id,
                "Маппингов нет.\n\n"
                "Открой /lots, выбери лот кнопкой 🎯, затем из /ns_cats или "
                "/ns_search нажми «Замапить» на нужной услуге.",
                reply_markup=ui.single_close_kb(),
            )
            return
        await self._render_paginated_from_cmd(msg, kind="mappings", sid=sid, page=0)

    @_guard
    async def _do_groups(self, msg: Message) -> None:
        sid, total = await self._collect_groups()
        if total == 0:
            await self._send_view(
                msg.chat.id,
                "Группы ещё не созданы.",
                reply_markup=ui.single_close_kb(),
            )
            return
        await self._render_paginated_from_cmd(msg, kind="groups", sid=sid, page=0)

    @_guard
    async def _show_mappings_via_cq(self, cq: CallbackQuery) -> None:
        sid, total = await self._collect_mappings()
        if total == 0:
            await self._edit_or_answer(
                cq,
                "Маппингов нет.\n\n"
                "Открой <b>Лоты FunPay</b> в меню, выбери лот кнопкой 🎯, "
                "затем в <b>Каталоге NS</b> найди нужную услугу и нажми «Замапить».",
                reply_markup=ui.single_close_kb(),
            )
            return
        await self._render_paginated(cq, kind="mappings", sid=sid, page=0)

    @_guard
    async def _show_groups_via_cq(self, cq: CallbackQuery) -> None:
        sid, total = await self._collect_groups()
        if total == 0:
            await self._edit_or_answer(
                cq, "Группы ещё не созданы.", reply_markup=ui.single_close_kb()
            )
            return
        await self._render_paginated(cq, kind="groups", sid=sid, page=0)

    async def _collect_mappings(self) -> tuple[str, int]:
        async with session_factory()() as session:
            stmt = select(Mapping).order_by(Mapping.funpay_lot_id)
            mappings = list((await session.execute(stmt)).scalars().all())
            groups = {
                group.id: group.name
                for group in (await session.execute(select(LotGroup))).scalars().all()
            }
            for mapping in mappings:
                if mapping.group_id is not None:
                    setattr(mapping, "_group_name", groups.get(mapping.group_id))
        sid = self._sessions.put(mappings, title="mappings")
        return sid, len(mappings)

    async def _collect_groups(self) -> tuple[str, int]:
        async with session_factory()() as session:
            groups = await list_lot_groups(session)
            await session.commit()
            counts = {group.id: [0, 0] for group in groups}
            mappings = list((await session.execute(select(Mapping))).scalars().all())
            for mapping in mappings:
                if mapping.group_id in counts:
                    counts[mapping.group_id][0] += 1
                    if mapping.enabled:
                        counts[mapping.group_id][1] += 1
            for group in groups:
                count, active = counts.get(group.id, [0, 0])
                setattr(group, "_mappings_count", count)
                setattr(group, "_active_mappings_count", active)
        sid = self._sessions.put(groups, title="groups")
        return sid, len(groups)

    @_guard
    async def _do_ns_cats(self, msg: Message) -> None:
        try:
            stock = await self._get_stock()
        except Exception as exc:
            await self._send_view(
                msg.chat.id,
                f"NS get_stock упал: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        if not stock.categories:
            await self._send_view(
                msg.chat.id, "Каталог NS пустой.", reply_markup=ui.single_close_kb()
            )
            return
        sid = self._sessions.put(
            stock.categories,
            title="NS categories",
            meta={"chat_id": msg.chat.id},
        )
        await self._render_paginated_from_cmd(msg, kind="ns_cats", sid=sid, page=0)

    @_guard
    async def _show_ns_cats_via_cq(self, cq: CallbackQuery) -> None:
        try:
            stock = await self._get_stock()
        except Exception as exc:
            await self._edit_or_answer(
                cq,
                f"NS get_stock упал: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        if not stock.categories:
            await self._edit_or_answer(
                cq, "Каталог NS пустой.", reply_markup=ui.single_close_kb()
            )
            return
        sid = self._sessions.put(
            stock.categories,
            title="NS categories",
            meta={"chat_id": cq.from_user.id},
        )
        await self._render_paginated(cq, kind="ns_cats", sid=sid, page=0)

    @_guard
    async def _do_ns_search(self, msg: Message) -> None:
        text = (msg.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await self._send_view(
                msg.chat.id, ui.HINT_NS_SEARCH, reply_markup=ui.single_close_kb()
            )
            return
        query = parts[1].strip()
        try:
            stock = await self._get_stock()
        except Exception as exc:
            await self._send_view(
                msg.chat.id,
                f"NS get_stock упал: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        results = _filter_services(stock, query)
        if not results:
            await self._send_view(
                msg.chat.id,
                f"По запросу «{html.escape(query)}» ничего не нашёл.\n"
                "Попробуй /ns_cats — там виден весь каталог.",
                reply_markup=ui.single_close_kb(),
            )
            return
        sid = self._sessions.put(results, title=query, meta={"chat_id": msg.chat.id})
        await self._render_paginated_from_cmd(msg, kind="ns_search", sid=sid, page=0)

    @_guard
    async def _do_funpay_reconnect(self, msg: Message) -> None:
        if self._funpay_reconnect is None:
            await msg.answer("Реконнект не подключён в этой конфигурации.")
            return
        progress = await msg.answer("⏳ Переподключаю FunPay...")
        try:
            result = await self._funpay_reconnect()
        except Exception as exc:
            await progress.edit_text(f"❌ Реконнект упал: <code>{html.escape(str(exc))}</code>")
            return
        if result.get("connected"):
            await progress.edit_text(
                f"✅ FunPay подключён\n"
                f"id: <code>{result.get('account_id')}</code>\n"
                f"username: <b>{result.get('username') or '—'}</b>"
            )
        else:
            await progress.edit_text(
                "❌ FunPay всё ещё недоступен. Проверь cookies (golden_key, PHPSESSID).\n"
                "Диагностика: <code>python -m src.tools.check_funpay</code>"
            )

    @_guard
    async def _run_reconnect_via_cq(self, cq: CallbackQuery) -> None:
        if self._funpay_reconnect is None:
            await cq.answer("Реконнект не подключён", show_alert=True)
            return
        await self._edit_or_answer(cq, "⏳ Переподключаю FunPay...", reply_markup=None)
        try:
            result = await self._funpay_reconnect()
        except Exception as exc:
            await self._edit_or_answer(
                cq,
                f"❌ Реконнект упал: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        if result.get("connected"):
            await self._edit_or_answer(
                cq,
                f"✅ FunPay подключён\n"
                f"id: <code>{result.get('account_id')}</code>\n"
                f"username: <b>{result.get('username') or '—'}</b>",
                reply_markup=ui.single_close_kb(),
            )
        else:
            await self._edit_or_answer(
                cq,
                "❌ FunPay всё ещё недоступен. Проверь cookies в .env.",
                reply_markup=ui.single_close_kb(),
            )

    @_guard
    async def _do_calc(self, msg: Message) -> None:
        parts = (msg.text or "").split()
        if len(parts) < 2:
            await msg.answer(
                "Использование: <code>/calc &lt;funpay_lot_id&gt;</code>"
            )
            return
        try:
            funpay_lot_id = int(parts[1])
        except ValueError:
            await msg.answer("funpay_lot_id должен быть числом")
            return
        text, kb = await self._render_calc(funpay_lot_id)
        await msg.answer(text, reply_markup=kb)

    async def _render_calc_text(self, funpay_lot_id: int) -> str:
        """Совместимая обёртка: возвращает только текст (используется в тестах)."""
        text, _ = await self._render_calc(funpay_lot_id)
        return text

    async def _render_calc(self, funpay_lot_id: int):
        """
        Полный расчёт цены + клавиатура.
        Возвращает (text, reply_markup).
        """
        async with session_factory()() as session:
            stmt = select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
            mapping = (await session.execute(stmt)).scalar_one_or_none()
            group = (
                await session.get(LotGroup, mapping.group_id)
                if mapping is not None and mapping.group_id is not None
                else None
            )
        if mapping is None:
            return (
                f"Маппинга для лота <code>{funpay_lot_id}</code> нет.\n"
                "Сначала сделай маппинг через меню или <code>/map ...</code>.",
                ui.single_close_kb(),
            )
        try:
            stock = await self._get_stock()
        except Exception as exc:
            return (
                f"NS get_stock упал: <code>{html.escape(str(exc))}</code>",
                ui.single_close_kb(),
            )

        svc = None
        for cat in stock.categories:
            for s in cat.services:
                if s.service_id == mapping.ns_service_id:
                    svc = s
                    break
            if svc is not None:
                break
        if svc is None:
            return (
                f"NS service_id <code>{mapping.ns_service_id}</code> "
                "не найден в каталоге.",
                ui.single_close_kb(),
            )

        rate = await get_rate_breakdown(self._settings)
        from src.config_runtime import get_global_markup_percent, get_stock_cap
        eff_markup = await get_global_markup_percent(self._settings)
        eff_stock_cap = await get_stock_cap(self._settings)
        pricing = compute_pricing(
            ns_service=svc,
            mapping=mapping,
            settings=self._settings,
            fx_rate_usd_to_target=rate.effective,
            default_markup=eff_markup,
            default_stock_cap=eff_stock_cap,
            group_markup_percent=group.markup_percent if group is not None else None,
            group_stock_cap=group.stock_cap if group is not None else None,
        )
        current_seller: float | None = None
        if self._funpay_client is not None:
            try:
                summary = await asyncio.wait_for(
                    self._funpay_client.get_lot_summary(funpay_lot_id),
                    timeout=FP_TIMEOUT_SECONDS,
                )
                cs = summary.get("fields.price")
                current_seller = float(cs) if cs is not None else None
            except Exception:
                pass

        threshold = self._settings.price_update_threshold_percent
        if current_seller is None or current_seller <= 0:
            will_update = True
        else:
            diff = abs(pricing.round_price() - current_seller) / current_seller * 100
            will_update = diff >= threshold

        cur = self._settings.funpay_currency.value
        if rate.has_premium:
            rate_line = (
                f"Курс USD→{cur}: <b>{rate.effective:.4f}</b> "
                f"(биржа {rate.base:.4f} + {rate.premium_percent:.1f}%)\n"
            )
        else:
            rate_line = f"Курс USD→{cur}: <b>{rate.effective:.4f}</b>\n"

        # Источник наценки
        if mapping.markup_percent is not None:
            markup_source = (
                f"<b>{pricing.markup_percent}%</b> "
                f"(зашита в маппинге, глобально сейчас "
                f"{eff_markup:.2f}%)"
            )
        elif group is not None and group.markup_percent is not None:
            markup_source = (
                f"<b>{pricing.markup_percent}%</b> "
                f"(группа <b>{html.escape(group.name)}</b>; "
                f"глобально сейчас {eff_markup:.2f}%)"
            )
        else:
            env_default = self._settings.markup_percent
            if abs(eff_markup - env_default) > 1e-9:
                markup_source = (
                    f"<b>{pricing.markup_percent}%</b> "
                    f"(глобально, runtime-override "
                    f"<code>/setdefault markup</code>; в .env {env_default}%)"
                )
            else:
                markup_source = (
                    f"<b>{pricing.markup_percent}%</b> "
                    f"(глобально из <code>MARKUP_PERCENT</code>)"
                )

        text = (
            f"📊 <b>Расчёт цены для лота {funpay_lot_id}</b>\n\n"
            f"NS: <b>{pricing.ns_price_usd:.4f}</b> USD\n"
            f"<i>{svc.service_name[:60]}</i>\n\n"
            f"{rate_line}"
            f"Наценка: {markup_source}\n"
            f"Комиссия FunPay (справочно): <b>{pricing.commission_percent}%</b>\n\n"
            f"➡ Цена продавцу: <b>{pricing.round_price()} {cur}</b>\n"
            f"➡ Цена клиенту: <b>{pricing.round_client_price()} {cur}</b>\n"
            f"➡ Сток: <b>{pricing.stock}</b>"
        )
        if current_seller is not None:
            text += (
                f"\n\nТекущая цена продавца: <b>{current_seller}</b>\n"
                f"Обновлять при следующем sync? "
                f"<b>{'да' if will_update else 'нет (в пределах порога)'}</b>"
            )

        # Клавиатура: «сбросить наценку» появляется только если она зашита
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        rows: list[list[InlineKeyboardButton]] = []
        if mapping.markup_percent is not None:
            rows.append([
                InlineKeyboardButton(
                    text=f"♻ Сбросить наценку → {self._settings.markup_percent}%",
                    callback_data=f"calc:reset_markup:{funpay_lot_id}",
                ),
            ])
        rows.append([
            InlineKeyboardButton(
                text="🔄 Sync сейчас",
                callback_data=f"menu:{ui.MENU_KIND_SYNC}",
            ),
            InlineKeyboardButton(
                text="✖ Закрыть",
                callback_data="close",
            ),
        ])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _on_calc_click(self, cq: CallbackQuery) -> None:
        """
        calc:reset_markup:<funpay_lot_id> — обнулить markup_percent в маппинге,
        чтобы он начал использовать глобальный default.
        """
        parts = (cq.data or "").split(":")
        if len(parts) == 3 and parts[1] == "open":
            try:
                funpay_lot_id = int(parts[2])
            except ValueError:
                await cq.answer("Некорректный lot_id", show_alert=True)
                return
            text, kb = await self._render_calc(funpay_lot_id)
            await self._edit_or_answer(cq, text, reply_markup=kb)
            return

        if len(parts) != 3 or parts[1] != "reset_markup":
            await cq.answer("Неизвестная команда расчёта", show_alert=True)
            return
        try:
            funpay_lot_id = int(parts[2])
        except ValueError:
            await cq.answer("Некорректный lot_id", show_alert=True)
            return

        async with session_factory()() as session:
            stmt = select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
            obj = (await session.execute(stmt)).scalar_one_or_none()
            if obj is None:
                await cq.answer("Маппинг не найден", show_alert=True)
                return
            obj.markup_percent = None
            await session.commit()

        await cq.answer(
            f"♻ Маппинг лота {funpay_lot_id} теперь использует "
            f"глобальную наценку {self._settings.markup_percent}%",
            show_alert=True,
        )
        # Перерисуем то же сообщение со свежим расчётом
        text, kb = await self._render_calc(funpay_lot_id)
        await self._edit_or_answer(cq, text, reply_markup=kb)

    async def _on_newlot_click(self, cq: CallbackQuery) -> None:
        """
        Обработка кнопок под Telegram-уведомлением о новом лоте.

        newlot:target:<lot_id>  — выставить лот целью (две-кликовый маппинг)
        newlot:inspect:<lot_id> — показать карточку Inspect лота
        """
        parts = (cq.data or "").split(":")
        if len(parts) not in (3, 4):
            await cq.answer("Некорректный callback", show_alert=True)
            return
        action = parts[1]
        try:
            lot_id = int(parts[2])
        except ValueError:
            await cq.answer("Некорректный lot_id", show_alert=True)
            return

        if action == "target":
            label = await self._known_lot_label(lot_id, cq)
            self._target_lots[cq.from_user.id] = lot_id
            self._target_labels[cq.from_user.id] = label[:60]
            await cq.answer(
                f"🎯 Цель: {label[:80]}\nТеперь открой 🗂 Каталог NS или /ns_search "
                "и нажми ✅ на нужной услуге.",
                show_alert=True,
            )
            return

        if action == "inspect":
            if self._funpay_client is None:
                await cq.answer("FunPay не подключён", show_alert=True)
                return
            try:
                summary = await asyncio.wait_for(
                    self._funpay_client.get_lot_summary(lot_id),
                    timeout=FP_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                await cq.answer(
                    f"Ошибка: {str(exc)[:120]}", show_alert=True
                )
                return
            text = _format_inspect(lot_id, summary)
            await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())
            return

        if action == "map":
            if len(parts) != 4:
                await cq.answer("Некорректный callback map", show_alert=True)
                return
            try:
                service_id = int(parts[3])
            except ValueError:
                await cq.answer("Некорректный ns_service_id", show_alert=True)
                return
            await cq.answer("Проверяю NS-услугу и маппинг...", show_alert=False)
            try:
                stock = await self._get_stock()
            except Exception as exc:
                if cq.message is not None:
                    await cq.message.answer(
                        f"❌ NS stock упал: <code>{html.escape(str(exc))[:300]}</code>",
                        reply_markup=ui.single_close_kb(),
                    )
                return
            svc = None
            for cat in stock.categories:
                for item in cat.services:
                    if item.service_id == service_id:
                        svc = item
                        break
                if svc is not None:
                    break
            if svc is None:
                if cq.message is not None:
                    await cq.message.answer(
                        "❌ NS-услуга больше не найдена. Обнови каталог или используй /ns_search.",
                        reply_markup=ui.single_close_kb(),
                    )
                return
            try:
                label = await self._known_lot_label(lot_id, cq)
                self._target_labels[cq.from_user.id] = label
                await self._save_mapping_via_cq(
                    cq,
                    funpay_lot_id=lot_id,
                    ns_service=svc,
                    answer_callback=False,
                )
            finally:
                self._target_labels.pop(cq.from_user.id, None)
            return

        await cq.answer(f"Неизвестное newlot-действие: {action}", show_alert=True)

    async def _on_mapconfirm_click(self, cq: CallbackQuery) -> None:
        parts = (cq.data or "").split(":")
        if len(parts) != 3:
            await cq.answer("Некорректное подтверждение", show_alert=True)
            return
        try:
            funpay_lot_id = int(parts[1])
            service_id = int(parts[2])
        except ValueError:
            await cq.answer("Некорректные id", show_alert=True)
            return
        try:
            stock = await self._get_stock()
        except Exception as exc:
            await cq.answer(f"NS stock упал: {str(exc)[:120]}", show_alert=True)
            return
        svc = None
        for cat in stock.categories:
            for item in cat.services:
                if item.service_id == service_id:
                    svc = item
                    break
            if svc is not None:
                break
        if svc is None:
            await cq.answer("NS-услуга больше не найдена", show_alert=True)
            return
        async with session_factory()() as session:
            row = await session.get(KnownLot, funpay_lot_id)
            if row is not None and row.title:
                self._target_labels[cq.from_user.id] = row.title[:120]
        await self._save_mapping_via_cq(
            cq, funpay_lot_id=funpay_lot_id, ns_service=svc, force=True
        )

    async def _on_settings_click(self, cq: CallbackQuery) -> None:
        """
        settings:<param>:<delta>
            settings:markup:+0.5  →  бамп текущего эффективного markup на +0.5%
            settings:premium:-1   →  ...
        """
        parts = (cq.data or "").split(":")
        if len(parts) != 3:
            await cq.answer("Некорректный callback", show_alert=True)
            return
        param = parts[1]
        try:
            delta = float(parts[2].replace(",", "."))
        except ValueError:
            await cq.answer("Некорректное число", show_alert=True)
            return

        from src.config_runtime import (
            get_global_markup_percent,
            get_premium_percent,
            set_global_markup_percent,
            set_premium_percent,
        )
        try:
            if param == "markup":
                cur = await get_global_markup_percent(self._settings)
                new_val = round(cur + delta, 4)
                await set_global_markup_percent(new_val)
                await cq.answer(
                    f"Markup: {cur:.2f}% → {new_val:.2f}%", show_alert=False
                )
            elif param == "premium":
                cur = await get_premium_percent(self._settings)
                new_val = round(cur + delta, 4)
                await set_premium_percent(new_val)
                await cq.answer(
                    f"Premium: {cur:.2f}% → {new_val:.2f}%", show_alert=False
                )
            else:
                await cq.answer(
                    f"Неизвестный settings-param: {param}", show_alert=True
                )
                return
        except ValueError as exc:
            await cq.answer(f"Не принято: {exc}", show_alert=True)
            return

        # Перерисуем то же сообщение свежими настройками
        if cq.message is not None:
            try:
                await self._do_show_settings(cq.message)  # type: ignore[arg-type]
                with suppress_telegram():
                    await cq.message.delete()
            except Exception:
                pass

    async def _on_group_click(self, cq: CallbackQuery) -> None:
        parts = (cq.data or "").split(":")
        if len(parts) < 3:
            await cq.answer("Некорректная команда группы", show_alert=True)
            return
        action = parts[1]
        try:
            target_id = int(parts[2])
        except ValueError:
            await cq.answer("Некорректный id", show_alert=True)
            return

        if action == "assign_none":
            async with session_factory()() as session:
                mapping = await session.get(Mapping, target_id)
                if mapping is None:
                    await cq.answer("Маппинг не найден", show_alert=True)
                    return
                await assign_mapping_group(session, mapping, None)
                await session.commit()
            await cq.answer("Группа сброшена", show_alert=False)
            await self._show_mappings_via_cq(cq)
            return

        if action == "preview":
            await cq.answer("Считаю preview группы...", show_alert=False)
            text = await self._render_group_preview(target_id)
            await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())
            return

        async with session_factory()() as session:
            group = await session.get(LotGroup, target_id)
            if group is None:
                await cq.answer("Группа не найдена", show_alert=True)
                return

            if action == "markup_default":
                group.markup_percent = None
            elif action == "markup_set":
                if len(parts) < 4:
                    await cq.answer("Не указан процент", show_alert=True)
                    return
                group.markup_percent = float(parts[3].replace(",", "."))
            elif action == "markup_delta":
                if len(parts) < 4:
                    await cq.answer("Не указан шаг", show_alert=True)
                    return
                base = group.markup_percent
                if base is None:
                    base = self._settings.markup_percent
                group.markup_percent = max(0.0, base + float(parts[3].replace(",", ".")))
            else:
                await cq.answer("Неизвестная команда группы", show_alert=True)
                return
            await session.commit()
            shown = format_percent(group.markup_percent)
            group_name = group.name

        await cq.answer(
            f"{group_name}: markup {shown if shown != '—' else 'global'}",
            show_alert=False,
        )
        await self._show_groups_via_cq(cq)

    @_guard
    async def _do_setmarkup(self, msg: Message) -> None:
        """
        /setmarkup <funpay_lot_id> <percent>
        Меняет наценку конкретного маппинга, не трогая остальное.
        """
        parts = (msg.text or "").strip().split()
        if len(parts) < 3:
            await msg.answer(
                "Использование: <code>/setmarkup &lt;funpay_lot_id&gt; &lt;percent&gt;</code>\n\n"
                "Пример: <code>/setmarkup 69300023 6</code>\n"
                "Чтобы вернуть default — <code>/setmarkup 69300023 default</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        try:
            funpay_lot_id = int(parts[1])
        except ValueError:
            await msg.answer("funpay_lot_id должен быть числом")
            return
        raw = parts[2].lower().replace(",", ".").rstrip("%")
        markup: float | None
        if raw in ("default", "none", "-", ""):
            markup = None
        else:
            try:
                markup = float(raw)
            except ValueError:
                await msg.answer(f"Не могу распарсить markup «{parts[2]}»")
                return

        async with session_factory()() as session:
            stmt = select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
            obj = (await session.execute(stmt)).scalar_one_or_none()
            if obj is None:
                await msg.answer(
                    f"Маппинг для лота <code>{funpay_lot_id}</code> не найден."
                )
                return
            obj.markup_percent = markup
            await session.commit()

        from src.config_runtime import get_global_markup_percent
        eff_default = await get_global_markup_percent(self._settings)
        if markup is None:
            shown = f"default ({format_percent(eff_default)}%)"
            note = ""
        else:
            shown = (
                f"{format_percent(markup)}% "
                f"(default {format_percent(eff_default)}%)"
            )
            if abs(markup - eff_default) < 1e-6:
                note = (
                    "\n\nℹ Эта наценка <b>равна default</b> — sync покажет "
                    "<code>updated=0</code>, потому что цена не изменится. "
                    "Чтобы избавиться от персонального оверрайда — "
                    f"<code>/setmarkup {funpay_lot_id} default</code>."
                )
            else:
                note = ""
        base_text = (
            f"✏ Markup для лота <code>{funpay_lot_id}</code>: <b>{shown}</b>\n\n"
            f"{note}"
        )

        if self._sync_trigger is None:
            await msg.answer(
                base_text
                + "\n\nЗапусти 🔄 Синхронизация или /sync, чтобы новая цена применилась.",
                reply_markup=ui.single_close_kb(),
            )
            return

        progress = await msg.answer(
            base_text + "\n\n⏳ Сразу запускаю sync, чтобы применить цену...",
            reply_markup=ui.single_close_kb(),
        )
        try:
            result = await self._sync_trigger()
        except Exception as exc:
            logger.exception("Sync after setmarkup failed")
            await progress.edit_text(
                base_text
                + "\n\n❌ Sync после setmarkup упал: "
                f"<code>{html.escape(str(exc))[:300]}</code>\n"
                "Наценка сохранена; можно повторить /sync вручную.",
                reply_markup=ui.single_close_kb(),
            )
            return

        checked = result.get("checked", 0)
        updated = result.get("updated", 0)
        skipped = result.get("skipped", 0)
        if updated:
            sync_note = "✅ Цена/остаток применены на FunPay."
        else:
            sync_note = (
                "ℹ Sync не внёс изменений: текущая цена на FunPay уже совпадает "
                "с расчётом или отличается меньше чем на видимую единицу."
            )
        await progress.edit_text(
            base_text
            + "\n\n"
            + sync_note
            + f"\nSync: checked={checked}, updated={updated}, skipped={skipped}",
            reply_markup=ui.single_close_kb(),
        )

    @_guard
    async def _do_reset_markups(self, msg: Message) -> None:
        """
        /reset_markups [percent] — обнулить markup_percent у всех маппингов,
        чтобы они начали использовать глобальный default. Если передать percent,
        сначала меняем runtime default, затем сбрасываем маппинги на него.
        """
        parts = (msg.text or "").strip().split()
        requested_markup: float | None = None
        if len(parts) >= 2:
            raw = parts[1].lower().replace(",", ".").rstrip("%")
            try:
                requested_markup = float(raw)
            except (TypeError, ValueError):
                await msg.answer(
                    "Использование: <code>/reset_markups [percent]</code>\n\n"
                    "Примеры:\n"
                    "<code>/reset_markups</code> — сбросить маппинги на текущий global\n"
                    "<code>/reset_markups 5</code> — сделать global 5% и сбросить маппинги",
                    reply_markup=ui.single_close_kb(),
                )
                return
            if requested_markup < 0 or requested_markup > 200:
                await msg.answer(
                    "Наценка должна быть в диапазоне <b>0..200%</b>.",
                    reply_markup=ui.single_close_kb(),
                )
                return

        if requested_markup is not None:
            from src.config_runtime import set_global_markup_percent
            await set_global_markup_percent(requested_markup)

        async with session_factory()() as session:
            stmt = select(Mapping)
            rows = list((await session.execute(stmt)).scalars().all())
            affected = 0
            for obj in rows:
                if obj.markup_percent is not None:
                    obj.markup_percent = None
                    affected += 1
            if affected > 0:
                await session.commit()

        from src.config_runtime import get_global_markup_percent
        eff = await get_global_markup_percent(self._settings)
        prefix = (
            f"✅ Глобальная наценка установлена: <b>{eff:.2f}%</b>.\n"
            if requested_markup is not None else ""
        )
        await msg.answer(
            prefix + (
                (
                    f"♻ Сброшено наценок: <b>{affected}</b> "
                    f"(из {len(rows)} маппингов).\n"
                    f"Теперь все используют глобальную <b>{eff:.2f}%</b>.\n\n"
                    "Запусти 🔄 Синхронизация или /sync, чтобы новые цены применились."
                ) if affected > 0 else (
                    f"Все {len(rows)} маппингов уже используют глобальную "
                    f"наценку <b>{eff:.2f}%</b> — менять нечего."
                )
            ),
            reply_markup=ui.single_close_kb(),
        )

    @_guard
    async def _do_setdefault(self, msg: Message) -> None:
        """
        /setdefault <markup|premium|stockcap> <value|default>

        Меняет один из глобальных параметров на лету, без перезапуска.
        Перекрывает соответствующий .env-параметр.
        """
        parts = (msg.text or "").strip().split()
        if len(parts) < 3:
            await msg.answer(
                "Использование: <code>/setdefault &lt;param&gt; &lt;value|default&gt;</code>\n\n"
                "Параметры:\n"
                "  <b>markup</b> — глобальная наценка FunPay, % (0..200)\n"
                "  <b>premium</b> — премия к курсу USD, % (0..50)\n"
                "  <b>stockcap</b> — лимит остатков на FunPay (1..100000)\n"
                "  <b>shop_markup</b> — наценка TG-shop, % (0..100)\n"
                "  <b>shop_referral</b> — % реф-кэшбэка (0..100)\n\n"
                "Примеры:\n"
                "<code>/setdefault markup 5</code>\n"
                "<code>/setdefault premium 3</code>\n"
                "<code>/setdefault stockcap 50</code>\n"
                "<code>/setdefault shop_markup 10</code>\n"
                "<code>/setdefault markup default</code> — вернуть к .env",
                reply_markup=ui.single_close_kb(),
            )
            return

        param = parts[1].lower()
        raw = parts[2].lower().replace(",", ".").rstrip("%")
        from src.config_runtime import (
            set_global_markup_percent,
            set_premium_percent,
            set_stock_cap,
            set_shop_markup_percent,
            set_shop_referral_percent,
            get_global_markup_percent,
            get_premium_percent,
            get_stock_cap,
            get_shop_markup_percent,
            get_shop_referral_percent,
        )

        try:
            if param == "markup":
                if raw in ("default", "none", "-", ""):
                    await set_global_markup_percent(None)
                else:
                    await set_global_markup_percent(float(raw))
                eff = await get_global_markup_percent(self._settings)
                env_v = self._settings.markup_percent
                shown = (
                    f"<b>{eff:.2f}%</b> "
                    + ("(runtime override)" if abs(eff - env_v) > 1e-9 else "(из .env)")
                )
                hint = (
                    "Это default для всех маппингов с markup=NULL. "
                    "У маппингов с зашитой индивидуальной наценкой ничего не меняется."
                )
            elif param == "premium":
                if raw in ("default", "none", "-", ""):
                    await set_premium_percent(None)
                else:
                    await set_premium_percent(float(raw))
                eff = await get_premium_percent(self._settings)
                env_v = self._settings.usd_rub_premium_percent
                shown = (
                    f"<b>{eff:.2f}%</b> "
                    + ("(runtime override)" if abs(eff - env_v) > 1e-9 else "(из .env)")
                )
                hint = "Применяется только в режиме AUTO курса USD/RUB."
            elif param in ("stockcap", "stock_cap", "stock"):
                if raw in ("default", "none", "-", ""):
                    await set_stock_cap(None)
                else:
                    await set_stock_cap(int(float(raw)))
                eff = await get_stock_cap(self._settings)
                env_v = self._settings.funpay_stock_cap
                shown = (
                    f"<b>{eff}</b> "
                    + ("(runtime override)" if eff != env_v else "(из .env)")
                )
                hint = "Лимит остатков для маппингов с stock_cap=NULL."
            elif param in ("shop_markup", "shopmarkup"):
                if raw in ("default", "none", "-", ""):
                    await set_shop_markup_percent(None)
                else:
                    await set_shop_markup_percent(float(raw))
                eff = await get_shop_markup_percent(self._settings)
                env_v = self._settings.shop_markup_percent
                shown = (
                    f"<b>{eff:.2f}%</b> "
                    + ("(runtime override)" if abs(eff - env_v) > 1e-9 else "(из .env)")
                )
                hint = (
                    "Наценка для shop-каталога. Применится при следующем "
                    f"sync каталога (≤{self._settings.shop_catalog_refresh_seconds}с)."
                )
            elif param in ("shop_referral", "shopreferral", "shop_ref"):
                if raw in ("default", "none", "-", ""):
                    await set_shop_referral_percent(None)
                else:
                    await set_shop_referral_percent(float(raw))
                eff = await get_shop_referral_percent(self._settings)
                env_v = self._settings.shop_referral_percent
                shown = (
                    f"<b>{eff:.2f}%</b> "
                    + ("(runtime override)" if abs(eff - env_v) > 1e-9 else "(из .env)")
                )
                hint = (
                    "% от каждой покупки реферала, который идёт на "
                    "внутренний баланс пригласившего."
                )
            else:
                await msg.answer(
                    f"Неизвестный параметр «{param}». "
                    "Доступные: markup, premium, stockcap, shop_markup, shop_referral.",
                    reply_markup=ui.single_close_kb(),
                )
                return
        except (ValueError, TypeError) as exc:
            await msg.answer(
                f"Ошибка валидации: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return

        warn = ""
        if not self._settings.enable_real_actions:
            warn = (
                "\n\n🔴 <b>ENABLE_REAL_ACTIONS=false</b> — sync будет считать "
                "новые цены, но <b>не запишет</b> их на FunPay. "
                "Поставь true и рестартни сервис, чтобы цена реально менялась."
            )
        await msg.answer(
            f"✅ <b>{param}</b> = {shown}\n\n<i>{hint}</i>\n\n"
            "Применится при следующем sync (~30 c) или нажми 🔄 Синхронизация." + warn,
            reply_markup=ui.single_close_kb(),
        )

    @_guard
    async def _do_show_settings(self, msg: Message) -> None:
        """Показать активные runtime-настройки и .env-исходники."""
        from src.config_runtime import (
            get_global_markup_percent, get_premium_percent, get_stock_cap,
            get_shop_markup_percent, get_shop_referral_percent,
            get_overrides_snapshot,
        )
        eff_markup = await get_global_markup_percent(self._settings)
        eff_premium = await get_premium_percent(self._settings)
        eff_stock = await get_stock_cap(self._settings)
        eff_shop_markup = await get_shop_markup_percent(self._settings)
        eff_shop_referral = await get_shop_referral_percent(self._settings)
        overrides = await get_overrides_snapshot()

        def src(env_val, override_val):
            return "<i>override</i>" if override_val is not None else "<i>из .env</i>"

        shop_line = ""
        if self._settings.shop_enabled:
            shop_line = (
                "\n🛒 <b>TG-shop</b>\n"
                f"   Наценка: <b>{eff_shop_markup:.2f}%</b> "
                f"{src(self._settings.shop_markup_percent, overrides.get('shop_markup_percent'))}"
                f" (в .env: {self._settings.shop_markup_percent}%)\n"
                f"   Реф-кэшбэк: <b>{eff_shop_referral:.2f}%</b> "
                f"{src(self._settings.shop_referral_percent, overrides.get('shop_referral_percent'))}"
                f" (в .env: {self._settings.shop_referral_percent}%)\n"
                f"   Обновление каталога: каждые "
                f"<b>{self._settings.shop_catalog_refresh_seconds}с</b>\n"
            )

        text = (
            "🔧 <b>Текущие настройки</b>\n\n"
            f"📈 Наценка: <b>{eff_markup:.2f}%</b> {src(self._settings.markup_percent, overrides['global_markup_percent'])}\n"
            f"     в .env: {self._settings.markup_percent}%\n"
            f"💱 Премия к USD: <b>{eff_premium:.2f}%</b> {src(self._settings.usd_rub_premium_percent, overrides['usd_rub_premium_percent'])}\n"
            f"     в .env: {self._settings.usd_rub_premium_percent}%\n"
            f"🏦 Вывод FunPay: <b>{self._settings.funpay_withdrawal_fee_percent:.2f}%</b> <i>из .env</i>\n"
            f"📦 Лимит остатков: <b>{eff_stock}</b> {src(self._settings.funpay_stock_cap, overrides['funpay_stock_cap'])}\n"
            f"     в .env: {self._settings.funpay_stock_cap}\n"
            f"{shop_line}\n"
            f"⏱ Sync каждые: <b>{self._settings.sync_interval_seconds}с</b>\n"
            f"🔁 Discovery новых лотов: <b>{self._settings.new_lots_check_interval_seconds}с</b>\n\n"
            "Меняй на лету:\n"
            "<code>/setdefault markup 5</code>\n"
            "<code>/setdefault premium 3</code>\n"
            "<code>/setdefault stockcap 50</code>\n"
            "<code>/setdefault &lt;param&gt; default</code> — сбросить override"
        )
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        delta_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📈 −0.5%", callback_data="settings:markup:-0.5"
                ),
                InlineKeyboardButton(
                    text="📈 +0.5%", callback_data="settings:markup:+0.5"
                ),
                InlineKeyboardButton(
                    text="📈 −1%", callback_data="settings:markup:-1"
                ),
                InlineKeyboardButton(
                    text="📈 +1%", callback_data="settings:markup:+1"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💱 −0.5%", callback_data="settings:premium:-0.5"
                ),
                InlineKeyboardButton(
                    text="💱 +0.5%", callback_data="settings:premium:+0.5"
                ),
                InlineKeyboardButton(
                    text="🔄 Sync", callback_data=f"menu:{ui.MENU_KIND_SYNC}"
                ),
                InlineKeyboardButton(text="✖ Закрыть", callback_data="close"),
            ],
        ])
        await msg.answer(text, reply_markup=delta_kb)

    @_guard
    async def _do_force_sync(self, msg: Message) -> None:
        """
        /force_sync <funpay_lot_id>

        Полный одношаговый прогон по одному маппингу — для отладки, когда
        не понятно «почему цена не меняется». Печатает каждый шаг:
            1. маппинг найден
            2. NS-сервис найден
            3. курс и наценка
            4. целевая цена/сток
            5. текущая цена/сток на FunPay
            6. что будет сделано
            7. mode (REAL / DRY-RUN) и применил ли реально
        """
        parts = (msg.text or "").strip().split()
        if len(parts) < 2:
            await msg.answer(
                "Использование: <code>/force_sync &lt;funpay_lot_id&gt;</code>\n\n"
                "Команда сделает развёрнутый прогон одного лота и покажет, "
                "почему цена обновится или не обновится.",
                reply_markup=ui.single_close_kb(),
            )
            return
        try:
            lot_id = int(parts[1])
        except ValueError:
            await msg.answer("funpay_lot_id должен быть числом")
            return

        from src.config_runtime import (
            get_global_markup_percent, get_stock_cap,
        )
        from src.mapping.rules import compute_pricing
        from src.sync.fx import get_rate_breakdown
        from src.sync.stock_sync import (
            _decide_for_one, _apply_decision, _flatten_services,
        )

        # 1. маппинг
        async with session_factory()() as session:
            mapping = (await session.execute(
                select(Mapping).where(Mapping.funpay_lot_id == lot_id)
            )).scalar_one_or_none()
            group = (
                await session.get(LotGroup, mapping.group_id)
                if mapping is not None and mapping.group_id is not None
                else None
            )

        if mapping is None:
            await msg.answer(
                f"❌ Маппинга для лота <code>{lot_id}</code> нет.",
                reply_markup=ui.single_close_kb(),
            )
            return

        if not mapping.enabled:
            await msg.answer(
                f"⏸ Маппинг есть, но <b>enabled=False</b>. "
                "Включи в /mappings или /map ещё раз.",
                reply_markup=ui.single_close_kb(),
            )
            return

        if self._funpay_client is None:
            await msg.answer(
                "❌ FunPay-клиент не подключён. /funpay_reconnect.",
                reply_markup=ui.single_close_kb(),
            )
            return

        await msg.answer(
            f"⏳ Прогоняю force_sync для лота <code>{lot_id}</code> ...",
            reply_markup=None,
        )

        try:
            stock = await self._get_stock(force=True)
        except Exception as exc:
            await msg.answer(
                f"❌ NS get_stock упал: <code>{html.escape(str(exc))}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        services = _flatten_services(stock)
        ns_svc = services.get(mapping.ns_service_id)

        rate = await get_rate_breakdown(self._settings)
        eff_markup = await get_global_markup_percent(self._settings)
        eff_stock_cap = await get_stock_cap(self._settings)

        decision = await _decide_for_one(
            ns_svc, mapping, self._settings, rate.effective,
            self._funpay_client,
            effective_markup=eff_markup,
            effective_stock_cap=eff_stock_cap,
            group=group,
        )
        if decision is None:
            await msg.answer(
                "❌ _decide_for_one вернул None (неожиданная ошибка)",
                reply_markup=ui.single_close_kb(),
            )
            return

        if decision.skip_reason:
            await msg.answer(
                f"⚠ <b>Skip:</b> <code>{html.escape(decision.skip_reason)}</code>\n"
                f"Лот не будет обновлён, см. подробности в логе сервера.",
                reply_markup=ui.single_close_kb(),
            )
            return

        actions: list[str] = []
        if decision.will_update_price:
            actions.append(
                f"price <b>{decision.current_price}</b> → "
                f"<b>{decision.target.round_price()}</b> "
                f"{decision.target.currency.value}"
            )
        if decision.will_update_stock:
            actions.append(f"stock → <b>{decision.target.stock}</b>")
        if decision.will_activate:
            actions.append("activate")
        if decision.will_deactivate:
            actions.append("deactivate")

        real = self._settings.enable_real_actions
        mode = "🟢 REAL" if real else "🔴 DRY-RUN"

        applied_line = ""
        if actions and real:
            try:
                await _apply_decision(decision, self._funpay_client, self._settings)
                applied_line = "✅ <b>Изменения отправлены на FunPay</b>"
            except Exception as exc:
                applied_line = (
                    f"❌ save_lot упал: <code>{html.escape(str(exc))[:200]}</code>"
                )
        elif actions and not real:
            applied_line = (
                "🟡 <b>DRY-RUN</b> — изменения НЕ отправлены на FunPay.\n"
                "В <code>.env</code> поставь <code>ENABLE_REAL_ACTIONS=true</code> "
                "и перезапусти сервис."
            )
        else:
            applied_line = "ℹ Цена/сток уже в пределах порога — обновлять нечего."

        ns_line = (
            f"🛒 NS: <b>{ns_svc.service_name[:60]}</b> "
            f"(${ns_svc.price:.4f}, stock {ns_svc.in_stock})"
            if ns_svc is not None
            else "🛒 NS: <b>сервис не найден в каталоге!</b>"
        )

        text = (
            f"🔬 <b>force_sync для лота {lot_id}</b>\n\n"
            f"📌 Маппинг: ns_service_id=<code>{mapping.ns_service_id}</code>, "
            f"markup={'mapping ' + str(mapping.markup_percent) + '%' if mapping.markup_percent is not None else 'global'}\n"
            f"{ns_line}\n"
            f"💱 Курс: <b>{rate.effective:.4f}</b> "
            f"(база {rate.base:.4f} + {rate.premium_percent:.1f}%)\n"
            f"📈 Эффективная наценка: <b>{decision.target.markup_percent:.2f}%</b>\n\n"
            f"<b>Цель:</b>\n"
            f"  price (продавцу): <b>{decision.target.round_price()} "
            f"{decision.target.currency.value}</b>\n"
            f"  stock: <b>{decision.target.stock}</b>\n\n"
            f"<b>Сейчас на FunPay:</b>\n"
            f"  price: <b>{decision.current_price}</b>\n\n"
            f"<b>Решение sync:</b>\n  "
            + (", ".join(actions) if actions else "ничего не меняем")
            + f"\n\n<b>Режим:</b> {mode}\n{applied_line}"
        )
        await msg.answer(text, reply_markup=ui.single_close_kb())

    @_guard
    async def _do_funpay_check(self, msg: Message) -> None:
        """
        Диагностика FunPay-сессии: проверяет 3 уровня — golden_key,
        список лотов и админку одного лота. Подсказывает, что починить.
        """
        if self._funpay_client is None:
            await msg.answer(
                "❌ FunPay-клиент не подключён в этот процесс.",
                reply_markup=ui.single_close_kb(),
            )
            return

        await msg.answer("🩺 Проверяю FunPay-сессию...", reply_markup=None)

        lines: list[str] = ["🩺 <b>Диагностика FunPay-сессии</b>", ""]
        all_ok = True

        try:
            acc_id = self._funpay_client.account_id
            uname = self._funpay_client.username
            if acc_id and uname:
                lines.append(
                    f"1️⃣ golden_key: ✅ id=<code>{acc_id}</code>, "
                    f"<code>{uname}</code>"
                )
            else:
                lines.append(
                    "1️⃣ golden_key: ⚠ account.id или username пустые — "
                    "<b>обнови FUNPAY_GOLDEN_KEY в .env</b>."
                )
                all_ok = False
        except Exception as exc:
            lines.append(
                f"1️⃣ golden_key: ❌ <code>{html.escape(str(exc))[:120]}</code>"
            )
            all_ok = False

        any_lot_id: int | None = None
        try:
            lots = await asyncio.wait_for(
                self._funpay_client.get_my_lots(), timeout=FP_TIMEOUT_SECONDS,
            )
            if lots:
                lines.append(f"2️⃣ get_my_lots: ✅ найдено лотов: <b>{len(lots)}</b>")
                first = lots[0]
                for attr in ("id", "lot_id"):
                    v = getattr(first, attr, None)
                    if v is not None:
                        try:
                            any_lot_id = int(v)
                            break
                        except (TypeError, ValueError):
                            continue
            else:
                lines.append(
                    "2️⃣ get_my_lots: ⚠ <b>пусто</b>. Возможно, лоты не "
                    "опубликованы или сессия неполная."
                )
                all_ok = False
        except Exception as exc:
            lines.append(
                f"2️⃣ get_my_lots: ❌ <code>{html.escape(str(exc))[:180]}</code>"
            )
            all_ok = False

        if any_lot_id is not None:
            try:
                fields = await asyncio.wait_for(
                    self._funpay_client.get_lot_fields(any_lot_id),
                    timeout=FP_TIMEOUT_SECONDS,
                )
                price_attr = getattr(fields, "price", None)
                lines.append(
                    f"3️⃣ get_lot_fields(<code>{any_lot_id}</code>): ✅ "
                    f"price={price_attr}"
                )
            except Exception as exc:
                text = str(exc)
                hint = ""
                if "expecting value" in text.lower():
                    hint = (
                        "\n     <b>→ Это и есть та самая ошибка.</b> "
                        "PHPSESSID протух или не совпадает с golden_key.\n"
                        "     Открой funpay.com в браузере (там, где ты "
                        "вошёл), достань свежий PHPSESSID и golden_key из "
                        "cookies, замени в <code>/opt/funpay-ns-bot/.env</code>, "
                        "потом <code>systemctl restart funpay-ns-bot</code>."
                    )
                lines.append(
                    f"3️⃣ get_lot_fields(<code>{any_lot_id}</code>): ❌ "
                    f"<code>{html.escape(text)[:180]}</code>{hint}"
                )
                all_ok = False
        else:
            lines.append(
                "3️⃣ get_lot_fields: пропустил (нет ни одного lot_id из шага 2)"
            )

        lines.append("")
        if all_ok:
            lines.append(
                "✅ <b>Всё в порядке</b>. Бот сможет читать и записывать лоты."
            )
        else:
            lines.append(
                "❗ <b>Найдены проблемы.</b> Пока они не починены, sync не "
                "сможет обновлять лоты на FunPay (будет только считать цены)."
            )
        await msg.answer("\n".join(lines), reply_markup=ui.single_close_kb())

    @_guard
    async def _do_inspect_lot(self, msg: Message) -> None:
        parts = (msg.text or "").split()
        if len(parts) < 2:
            await msg.answer("Использование: <code>/inspect_lot &lt;funpay_lot_id&gt;</code>")
            return
        try:
            funpay_lot_id = int(parts[1])
        except ValueError:
            await msg.answer("funpay_lot_id должен быть числом")
            return
        if self._funpay_client is None:
            await msg.answer("FunPay не подключён")
            return
        try:
            summary = await asyncio.wait_for(
                self._funpay_client.get_lot_summary(funpay_lot_id),
                timeout=FP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            await msg.answer(f"Ошибка: <code>{html.escape(str(exc))}</code>")
            return
        text = _format_inspect(funpay_lot_id, summary)
        await msg.answer(text, reply_markup=ui.single_close_kb())

    @_guard
    async def _do_lot_status(self, msg: Message) -> None:
        """
        /lot_status <funpay_lot_id>

        Read-only диагностика одного лота. В отличие от /force_sync:
          * НЕ применяет изменения к FunPay (только show);
          * НЕ зависит от FunPay-GET (если 429 — всё равно покажет cache state);
          * показывает diff-cache: TTL fresh/stale, last_synced_*;
          * показывает capped-indicator: NS_stock vs effective cap;
          * подсказывает, что произойдёт на следующем sync-цикле.

        Используется в кейсах вроде «лот показывает 99 уже долго»:
        видно, либо это работа stock_cap (нормально), либо cache никогда
        не stale'ется (баг).
        """
        parts = (msg.text or "").strip().split()
        if len(parts) < 2:
            await msg.answer(
                "Использование: <code>/lot_status &lt;funpay_lot_id&gt;</code>\n\n"
                "Read-only диагностика лота (без применения изменений).",
                reply_markup=ui.single_close_kb(),
            )
            return
        try:
            lot_id = int(parts[1])
        except ValueError:
            await msg.answer("funpay_lot_id должен быть числом")
            return

        from src.config_runtime import (
            get_global_markup_percent, get_stock_cap,
        )
        from src.mapping.rules import compute_pricing
        from src.sync.fx import get_rate_breakdown
        from src.sync.stock_sync import _flatten_services

        # 1. маппинг + group из БД
        async with session_factory()() as session:
            mapping = (await session.execute(
                select(Mapping).where(Mapping.funpay_lot_id == lot_id)
            )).scalar_one_or_none()
            group = (
                await session.get(LotGroup, mapping.group_id)
                if mapping is not None and mapping.group_id is not None
                else None
            )

        if mapping is None:
            await msg.answer(
                f"❌ Маппинга для лота <code>{lot_id}</code> нет.\n"
                f"Используй /lots → 🎯 + /ns_search для создания.",
                reply_markup=ui.single_close_kb(),
            )
            return

        # 2. NS-сервис (без force, используем cache от get_stock — он
        # обновляется catalog_sync каждые 90с, для read-only достаточно).
        try:
            stock = await self._get_stock(force=False)
        except Exception as exc:
            await msg.answer(
                f"❌ NS get_stock упал: <code>{html.escape(str(exc))[:200]}</code>",
                reply_markup=ui.single_close_kb(),
            )
            return
        ns_svc = _flatten_services(stock).get(mapping.ns_service_id)

        # 3. курс + наценка + cap (effective)
        rate = await get_rate_breakdown(self._settings)
        eff_markup = await get_global_markup_percent(self._settings)
        eff_stock_cap = await get_stock_cap(self._settings)

        # 4. target (без FunPay-GET — чисто на основе NS+rate+cap)
        target = None
        capped_note = ""
        if ns_svc is not None:
            target = compute_pricing(
                ns_service=ns_svc,
                mapping=mapping,
                settings=self._settings,
                fx_rate_usd_to_target=rate.effective,
                default_markup=eff_markup,
                default_stock_cap=eff_stock_cap,
                group_markup_percent=group.markup_percent if group is not None else None,
                group_stock_cap=group.stock_cap if group is not None else None,
            )
            raw_ns_stock = int(ns_svc.in_stock or 0)
            if raw_ns_stock > target.stock and target.stock > 0:
                capped_note = (
                    f" ⚠ <b>capped</b>: NS={raw_ns_stock} > cap={target.stock}"
                )

        # 5. Diff-cache state
        last_at = getattr(mapping, "last_synced_at", None)
        last_price = getattr(mapping, "last_synced_price", None)
        last_stock = getattr(mapping, "last_synced_stock", None)
        last_active = getattr(mapping, "last_synced_active", None)
        ttl = int(getattr(self._settings, "sync_stock_diff_cache_ttl_seconds", 300))

        cache_status = "<i>never synced</i>"
        cache_fresh = False
        minutes_ago: float | None = None
        if last_at is not None:
            delta = (datetime.utcnow() - last_at).total_seconds()
            minutes_ago = delta / 60.0
            cache_fresh = delta < ttl
            cache_status = (
                f"{'🟢 fresh' if cache_fresh else '🟡 stale'} "
                f"({minutes_ago:.1f} мин назад, TTL={ttl}с)"
            )

        # 6. Сравнение target vs last_synced (что бы произошло в fast-path)
        cache_hit_predicted = False
        if target is not None and cache_fresh and last_price is not None and \
                last_stock is not None and last_active is not None:
            target_active = target.stock > 0
            if (
                abs(float(last_price) - float(target.round_price())) <= 0.005
                and int(last_stock) == int(target.stock)
                and bool(last_active) == bool(target_active)
            ):
                cache_hit_predicted = True

        # 7. Текущий FunPay state (опционально — может упасть 429)
        funpay_line = "<i>FunPay GET не пробовали (read-only)</i>"
        # Если кэш stale — реальное состояние ВАЖНО показать, иначе
        # пользователь не поймёт почему cache fast-path skip'ает.
        if not cache_fresh and self._funpay_client is not None:
            try:
                lf = await asyncio.wait_for(
                    self._funpay_client.get_lot_fields(lot_id),
                    timeout=FP_TIMEOUT_SECONDS,
                )
                fp_price = getattr(lf, "price", "?")
                fp_amount = getattr(lf, "amount", "?")
                fp_active = getattr(lf, "active", "?")
                funpay_line = (
                    f"<b>FunPay сейчас:</b> price=<code>{fp_price}</code>, "
                    f"stock=<code>{fp_amount}</code>, "
                    f"active=<code>{fp_active}</code>"
                )
            except Exception as exc:
                funpay_line = (
                    f"⚠ FunPay GET упал (читать продолжаем в фоне): "
                    f"<code>{html.escape(str(exc))[:120]}</code>"
                )

        # 8. Выносим вердикт
        if target is None:
            verdict = "❌ NS-сервис не найден в каталоге"
        elif cache_hit_predicted:
            verdict = (
                "🟢 <b>Cache hit ожидается</b> — fast-path skip, "
                "FunPay-GET не будет."
            )
        elif cache_fresh and target is not None:
            verdict = (
                "🟡 <b>Cache fresh, но target != last_synced</b> — "
                "что-то случилось, но cache не пустим. "
                "Дождёмся TTL или /force_sync."
            )
        else:
            verdict = (
                f"🔄 <b>Cache stale</b> — на следующем sync-цикле будет "
                f"FunPay-GET + потенциальный save_lot."
            )

        # 9. Сборка ответа
        ns_line = (
            f"🛒 <b>NS:</b> {html.escape(ns_svc.service_name[:60])} "
            f"(${ns_svc.price:.4f}, in_stock={ns_svc.in_stock})"
            if ns_svc is not None
            else "🛒 <b>NS:</b> сервис не найден в каталоге!"
        )
        markup_origin = (
            f"mapping {mapping.markup_percent}%"
            if mapping.markup_percent is not None else f"global {eff_markup}%"
        )
        cap_origin = (
            f"mapping {mapping.stock_cap}"
            if mapping.stock_cap is not None
            else (
                f"group {group.stock_cap}"
                if group is not None and group.stock_cap is not None
                else f"global {eff_stock_cap}"
            )
        )

        target_block = (
            (
                f"<b>Target (что бот хочет на FunPay):</b>\n"
                f"  price: <b>{target.round_price()} "
                f"{target.currency.value}</b>\n"
                f"  stock: <b>{target.stock}</b>{capped_note}\n"
            )
            if target is not None else
            "<b>Target:</b> не вычислен (нет NS-сервиса)\n"
        )

        cache_block = (
            f"<b>Diff-cache (last save_lot success):</b>\n"
            f"  price: <code>{last_price}</code>\n"
            f"  stock: <code>{last_stock}</code>\n"
            f"  active: <code>{last_active}</code>\n"
            f"  status: {cache_status}\n"
        )

        text = (
            f"🩹 <b>lot_status #{lot_id}</b>\n"
            f"<i>{html.escape(mapping.label or '—')[:70]}</i>\n\n"
            f"📌 ns_service_id=<code>{mapping.ns_service_id}</code>, "
            f"enabled=<code>{mapping.enabled}</code>\n"
            f"📈 markup: {markup_origin}\n"
            f"📦 cap: {cap_origin}\n"
            f"💱 USD/RUB: <b>{rate.effective:.4f}</b>\n\n"
            f"{ns_line}\n\n"
            f"{target_block}\n"
            f"{cache_block}\n"
            f"{funpay_line}\n\n"
            f"{verdict}"
        )
        await msg.answer(text, reply_markup=ui.single_close_kb())

    @_guard
    async def _do_map(self, msg: Message) -> None:
        parts = (msg.text or "").strip().split()
        if len(parts) < 3:
            await msg.answer(
                "Использование:\n"
                "<code>/map &lt;funpay_lot_id&gt; &lt;ns_service_id&gt; [markup%] [label]</code>\n\n"
                "Пример: <code>/map 69300023 20 15 Apple 2 USD</code>"
            )
            return
        try:
            funpay_lot_id = int(parts[1])
            ns_service_id = int(parts[2])
        except ValueError:
            await msg.answer("funpay_lot_id и ns_service_id должны быть числами")
            return
        markup: float | None = None
        if len(parts) >= 4:
            try:
                markup = float(parts[3].replace(",", ".").rstrip("%"))
            except ValueError:
                await msg.answer(f"Не могу распарсить markup '{parts[3]}'")
                return
        label = " ".join(parts[4:]) if len(parts) >= 5 else None
        await self._save_mapping(
            msg=msg,
            funpay_lot_id=funpay_lot_id,
            ns_service_id=ns_service_id,
            markup_percent=markup,
            label=label,
        )

    @_guard
    async def _do_unmap(self, msg: Message) -> None:
        parts = (msg.text or "").strip().split()
        if len(parts) < 2:
            await msg.answer("Использование: <code>/unmap &lt;funpay_lot_id&gt;</code>")
            return
        try:
            funpay_lot_id = int(parts[1])
        except ValueError:
            await msg.answer("funpay_lot_id должен быть числом")
            return
        async with session_factory()() as session:
            stmt = select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
            obj = (await session.execute(stmt)).scalar_one_or_none()
            if obj is None:
                await msg.answer(f"Маппинг для лота {funpay_lot_id} не найден")
                return
            obj.enabled = False
            await session.commit()
        await msg.answer(f"⏸ Маппинг для лота <code>{funpay_lot_id}</code> выключен")

    # ─────────────── действия из callback'ов ───────────────

    @_guard
    async def _act_fp_target(self, cq: CallbackQuery, lot) -> None:
        lot_id = _lot_id_of(lot)
        if not isinstance(lot_id, int):
            try:
                lot_id = int(lot_id)
            except (TypeError, ValueError):
                await cq.answer("Не могу прочитать lot_id", show_alert=True)
                return
        label = ui.funpay_lot_label(lot, max_len=60)
        self._target_lots[cq.from_user.id] = lot_id
        self._target_labels[cq.from_user.id] = label or f"#{lot_id}"
        await cq.answer(
            f"🎯 Цель: {label or '#' + str(lot_id)}\n"
            f"Теперь открой «🗂 Каталог NS» или /ns_search и нажми ✅ на услуге.",
            show_alert=True,
        )

    @_guard
    async def _act_ns_map(self, cq: CallbackQuery, svc) -> None:
        target = self._target_lots.get(cq.from_user.id)
        if target is None:
            await cq.answer(
                "Сначала открой «🛒 Лоты FunPay» и выбери лот кнопкой 🎯.",
                show_alert=True,
            )
            return
        await self._save_mapping_via_cq(cq, funpay_lot_id=target, ns_service=svc)

    @_guard
    async def _act_cat_open(self, cq: CallbackQuery, cat) -> None:
        services = list(getattr(cat, "services", []))
        if not services:
            await cq.answer("В категории нет услуг", show_alert=True)
            return
        sid = self._sessions.put(
            services,
            title=cat.category_name,
            meta={
                "category_id": cat.category_id,
                "category_name": cat.category_name,
                "chat_id": cq.from_user.id,
            },
        )
        await self._render_paginated(cq, kind="ns_cat_services", sid=sid, page=0)

    @_guard
    async def _act_lot_calc(self, cq: CallbackQuery, lot) -> None:
        lot_id = _lot_id_of(lot)
        try:
            lot_id_int = int(lot_id)
        except (TypeError, ValueError):
            await cq.answer("Не могу прочитать lot_id", show_alert=True)
            return
        await cq.answer("Считаю...", show_alert=False)
        text = await self._render_calc_text(lot_id_int)
        if cq.message is not None:
            await cq.message.answer(text, reply_markup=ui.single_close_kb())

    @_guard
    async def _act_lot_inspect(self, cq: CallbackQuery, lot) -> None:
        lot_id = _lot_id_of(lot)
        try:
            lot_id_int = int(lot_id)
        except (TypeError, ValueError):
            await cq.answer("Не могу прочитать lot_id", show_alert=True)
            return
        if self._funpay_client is None:
            await cq.answer("FunPay не подключён", show_alert=True)
            return
        await cq.answer("Читаю...", show_alert=False)
        try:
            summary = await asyncio.wait_for(
                self._funpay_client.get_lot_summary(lot_id_int),
                timeout=FP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if cq.message is not None:
                await cq.message.answer(
                    f"❌ Ошибка inspect: <code>{html.escape(str(exc))}</code>"
                )
            return
        if cq.message is not None:
            await cq.message.answer(
                _format_inspect(lot_id_int, summary), reply_markup=ui.single_close_kb()
            )

    @_guard
    async def _act_lot_card(self, cq: CallbackQuery, lot) -> None:
        lot_id = _lot_id_of(lot)
        try:
            lot_id_int = int(lot_id)
        except (TypeError, ValueError):
            await cq.answer("Не могу прочитать lot_id", show_alert=True)
            return
        await cq.answer("Открываю карточку...", show_alert=False)
        text = await self._render_lot_card(lot_id_int, lot=lot)
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Расчёт",
                    callback_data=f"calc:open:{lot_id_int}",
                ),
                InlineKeyboardButton(
                    text="🔬 Inspect",
                    callback_data=f"newlot:inspect:{lot_id_int}",
                ),
            ],
            [
                InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home"),
                InlineKeyboardButton(text="✖ Закрыть", callback_data="close"),
            ],
        ])
        await self._edit_or_answer(cq, text, reply_markup=kb)

    @_guard
    async def _act_map_toggle(self, cq: CallbackQuery, mapping) -> None:
        async with session_factory()() as session:
            stmt = select(Mapping).where(Mapping.id == mapping.id)
            obj = (await session.execute(stmt)).scalar_one_or_none()
            if obj is None:
                await cq.answer("Маппинг не найден", show_alert=True)
                return
            obj.enabled = not obj.enabled
            await session.commit()
            new_state = obj.enabled
        await cq.answer(
            f"{'✅ включён' if new_state else '⏸ выключен'}",
            show_alert=False,
        )
        await self._refresh_mappings_view(cq)

    @_guard
    async def _act_map_delete(self, cq: CallbackQuery, mapping) -> None:
        async with session_factory()() as session:
            stmt = select(Mapping).where(Mapping.id == mapping.id)
            obj = (await session.execute(stmt)).scalar_one_or_none()
            if obj is None:
                await cq.answer("Маппинг не найден", show_alert=True)
                return
            await session.delete(obj)
            await session.commit()
        await cq.answer("🗑 Маппинг удалён", show_alert=False)
        await self._refresh_mappings_view(cq)

    @_guard
    async def _act_group_open(self, cq: CallbackQuery, group: LotGroup) -> None:
        async with session_factory()() as session:
            groups = {
                row.id: row.name
                for row in (await session.execute(select(LotGroup))).scalars().all()
            }
            mappings = await list_mappings(
                session, only_enabled=False, group_id=group.id
            )
            for mapping in mappings:
                setattr(mapping, "_group_name", groups.get(mapping.group_id))
        sid = self._sessions.put(
            mappings,
            title=group.name,
            meta={"group_id": group.id, "group_name": group.name},
        )
        await self._render_paginated(cq, kind="group_mappings", sid=sid, page=0)

    @_guard
    async def _act_map_group(self, cq: CallbackQuery, mapping: Mapping) -> None:
        async with session_factory()() as session:
            groups = await list_lot_groups(session)
            await session.commit()
        sid = self._sessions.put(
            groups,
            title="assign group",
            meta={
                "mapping_id": mapping.id,
                "mapping_label": ui.mapping_label(mapping, max_len=32),
            },
        )
        await self._render_paginated(cq, kind="group_assign", sid=sid, page=0)

    @_guard
    async def _act_group_assign(self, cq: CallbackQuery, group: LotGroup) -> None:
        parts = (cq.data or "").split(":")
        if len(parts) != 4:
            await cq.answer("Некорректная команда группы", show_alert=True)
            return
        sid = parts[2]
        sess = self._sessions.get(sid)
        if sess is None:
            await cq.answer("Сессия устарела", show_alert=True)
            return
        mapping_id = sess.meta.get("mapping_id")
        if mapping_id is None:
            await cq.answer("Не найден mapping_id", show_alert=True)
            return
        async with session_factory()() as session:
            mapping = await session.get(Mapping, int(mapping_id))
            if mapping is None:
                await cq.answer("Маппинг не найден", show_alert=True)
                return
            await assign_mapping_group(session, mapping, group.id)
            await session.commit()
        await cq.answer(f"📁 Группа: {group.name}", show_alert=False)
        await self._show_mappings_via_cq(cq)

    @_guard
    async def _act_order_retry(self, cq: CallbackQuery, order) -> None:
        if self._order_retry is None:
            await cq.answer("Retry не подключён", show_alert=True)
            return
        if getattr(order, "status", None) not in ("pins_ready", "manual_hold"):
            await cq.answer("Retry доступен только для pins_ready/manual_hold", show_alert=True)
            return
        await cq.answer("Пробую доставить повторно...", show_alert=False)
        result = await self._order_retry(order.funpay_order_id)
        text = (
            f"🔁 <b>Retry заказа</b>\n"
            f"FunPay: <code>{html.escape(order.funpay_order_id)}</code>\n"
            f"Результат: <code>{html.escape(str(result))[:800]}</code>"
        )
        await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())

    @_guard
    async def _act_order_manual_done(self, cq: CallbackQuery, order) -> None:
        if getattr(order, "status", None) != "manual_hold":
            await cq.answer("Доступно только для manual_hold", show_alert=True)
            return
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, order.funpay_order_id)
            if db_order is None:
                await cq.answer("Заказ не найден", show_alert=True)
                return
            db_order.status = "delivered"
            db_order.error = "manual_delivered: оператор подтвердил ручную выдачу"
            await session.commit()
        await cq.answer("Отмечено как выданное вручную", show_alert=False)
        await self._show_problems_via_cq(cq)

    async def _on_hold_click(self, cq: CallbackQuery) -> None:
        """
        Обработчик кнопок на алерте manual_hold_required.

        Форматы:
            hold:retry:<funpay_order_id>  → force-retry через self._order_retry
            hold:done:<funpay_order_id>   → пометить delivered (ручная выдача)
            hold:show:<funpay_order_id>   → показать детали (текст алерта)
        """
        raw = (cq.data or "")
        parts = raw.split(":", 2)
        if len(parts) != 3:
            await cq.answer("Неверный формат", show_alert=True)
            return
        _, action, funpay_order_id = parts
        funpay_order_id = funpay_order_id.strip()
        if not funpay_order_id:
            await cq.answer("Пустой order_id", show_alert=True)
            return

        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, funpay_order_id)
        if order is None:
            await cq.answer("Заказ не найден в БД", show_alert=True)
            return

        if action == "retry":
            if self._order_retry is None:
                await cq.answer("Retry не подключён", show_alert=True)
                return
            if order.status not in ("pins_ready", "manual_hold"):
                await cq.answer(
                    f"Retry недоступен: статус {order.status}", show_alert=True
                )
                return
            await cq.answer("Пробую доставить повторно…", show_alert=False)
            result = await self._order_retry(funpay_order_id)
            text = (
                f"🔁 <b>Retry заказа</b>\n"
                f"FunPay: <code>{html.escape(funpay_order_id)}</code>\n"
                f"Результат: <code>{html.escape(str(result))[:800]}</code>"
            )
            await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())
            return

        if action == "done":
            # Идемпотентно: если уже delivered, ничего не ломаем.
            if order.status == "delivered":
                await cq.answer("Уже отмечен как delivered", show_alert=False)
                return
            if order.status != "manual_hold":
                await cq.answer(
                    f"Доступно только для manual_hold (сейчас {order.status})",
                    show_alert=True,
                )
                return
            async with session_factory()() as session:
                db_order = await find_order_by_funpay_id(session, funpay_order_id)
                if db_order is None:
                    await cq.answer("Заказ исчез", show_alert=True)
                    return
                db_order.status = "delivered"
                db_order.error = (
                    "manual_delivered: оператор подтвердил ручную выдачу из alert'a"
                )
                await session.commit()
            await cq.answer("Отмечено как выданное вручную", show_alert=False)
            return

        if action == "show":
            text = (
                f"ℹ️ <b>Детали заказа</b>\n"
                f"FunPay: <code>{html.escape(funpay_order_id)}</code>\n"
                f"NS: <code>{html.escape(order.ns_custom_id or '—')}</code>\n"
                f"Статус: <code>{html.escape(order.status)}</code>\n"
                f"Покупатель: {html.escape(order.buyer_username or '—')}\n"
                f"Лот FunPay: <code>{order.funpay_lot_id}</code>\n"
                f"Кол-во: {order.quantity}\n"
                f"Цена FunPay: {order.funpay_price_rub or '—'}\n"
                f"Цена NS: {order.ns_price_usd or '—'}\n"
                f"Создан: <code>{order.created_at.isoformat(timespec='seconds')}</code>\n"
                f"Обновлён: <code>{order.updated_at.isoformat(timespec='seconds')}</code>\n"
                f"Описание: <code>{html.escape((order.description or '—')[:240])}</code>\n"
                f"Ошибка: <code>{html.escape((order.error or '—')[:400])}</code>"
            )
            await self._edit_or_answer(cq, text, reply_markup=ui.single_close_kb())
            return

        await cq.answer(f"Неизвестное действие: {action}", show_alert=True)

    @_guard
    async def _act_problem_force_sync(self, cq: CallbackQuery, item) -> None:
        lot_id = getattr(item, "funpay_lot_id", None)
        try:
            lot_id = int(lot_id)
        except (TypeError, ValueError):
            await cq.answer("Не могу прочитать lot_id", show_alert=True)
            return
        await cq.answer("Считаю lot preview...", show_alert=False)
        text, kb = await self._render_calc(lot_id)
        await self._edit_or_answer(cq, text, reply_markup=kb)

    @_guard
    async def _act_problem_enable_mapping(self, cq: CallbackQuery, item) -> None:
        lot_id = getattr(item, "funpay_lot_id", None)
        try:
            lot_id = int(lot_id)
        except (TypeError, ValueError):
            await cq.answer("Не могу прочитать lot_id", show_alert=True)
            return
        async with session_factory()() as session:
            mapping = (
                await session.execute(
                    select(Mapping).where(Mapping.funpay_lot_id == lot_id)
                )
            ).scalar_one_or_none()
            if mapping is None:
                await cq.answer("Маппинг не найден", show_alert=True)
                return
            mapping.enabled = True
            await session.commit()
        await cq.answer(f"Mapping lot {lot_id} включён", show_alert=False)
        await self._show_problems_via_cq(cq)

    async def _refresh_mappings_view(self, cq: CallbackQuery) -> None:
        sid, total = await self._collect_mappings()
        if total == 0:
            await self._edit_or_answer(
                cq, "Маппингов нет.", reply_markup=ui.single_close_kb()
            )
            return
        await self._render_paginated(cq, kind="mappings", sid=sid, page=0)

    async def _save_mapping_via_cq(
        self,
        cq: CallbackQuery,
        *,
        funpay_lot_id: int,
        ns_service,
        force: bool = False,
        answer_callback: bool = True,
    ) -> None:
        fp_label = self._target_labels.get(cq.from_user.id) or f"#{funpay_lot_id}"
        warnings = mapping_risk_warnings(fp_label, getattr(ns_service, "service_name", None))
        if warnings and not force:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            text = (
                "⚠️ <b>Проверь маппинг перед сохранением</b>\n\n"
                f"FunPay <code>{funpay_lot_id}</code>\n"
                f"<i>{html.escape(fp_label)}</i>\n\n"
                f"NS#{ns_service.service_id}\n"
                f"<i>{html.escape(str(ns_service.service_name))}</i>\n\n"
                "<b>Почему остановил:</b>\n"
                + "\n".join(f"• {html.escape(item)}" for item in warnings)
                + "\n\nЕсли это точно правильная пара, нажми «✅ Подтвердить»."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=f"mapconfirm:{funpay_lot_id}:{ns_service.service_id}",
                    ),
                    InlineKeyboardButton(text="✖ Отмена", callback_data="target:clear"),
                ],
            ])
            if answer_callback:
                await cq.answer("Нужно подтверждение", show_alert=True)
            if cq.message is not None:
                await cq.message.answer(text, reply_markup=kb)
            return
        async with session_factory()() as session:
            group = await classify_lot_group(
                session,
                f"{fp_label} {getattr(ns_service, 'service_name', '')}",
            )
            obj = await upsert_mapping(
                session,
                funpay_lot_id=funpay_lot_id,
                ns_service_id=ns_service.service_id,
                markup_percent=None,
                stock_cap=None,
                ns_fields_template='{"quantity":"@QUANTITY"}',
                enabled=True,
                label=str(ns_service.service_name)[:80],
                group_id=group.id if group is not None else None,
            )
            await session.commit()
        svc_label = ui.short_title(ns_service.service_name, limit=40)
        self._clear_target(cq.from_user.id)
        group_line = (
            f"📁 Группа: <b>{html.escape(group.name)}</b>\n"
            if group is not None
            else "📁 Группа: <i>не определена</i>\n"
        )
        if answer_callback:
            await cq.answer(
                f"✅ Замаппил «{fp_label}» → {svc_label}",
                show_alert=True,
            )
        if cq.message is not None:
            await cq.message.answer(
                f"✅ <b>Маппинг сохранён</b>\n"
                f"FunPay <code>{funpay_lot_id}</code> · "
                f"<i>{html.escape(fp_label)}</i>\n"
                f"     ↓\n"
                f"NS#{obj.ns_service_id} · "
                f"<i>{html.escape(svc_label)}</i>\n\n"
                f"{group_line}"
                f"Markup: default ({self._settings.markup_percent}%)\n"
                f"🎯 Цель сброшена — можно выбирать следующий лот.\n"
                f"Запусти 🔄 Синхронизация чтобы применить.",
                reply_markup=ui.single_close_kb(),
            )

    async def _save_mapping(
        self,
        *,
        msg: Message,
        funpay_lot_id: int,
        ns_service_id: int,
        markup_percent: float | None,
        label: str | None,
    ) -> None:
        async with session_factory()() as session:
            group = await classify_lot_group(session, label)
            obj = await upsert_mapping(
                session,
                funpay_lot_id=funpay_lot_id,
                ns_service_id=ns_service_id,
                markup_percent=markup_percent,
                stock_cap=None,
                ns_fields_template='{"quantity":"@QUANTITY"}',
                enabled=True,
                label=label,
                group_id=group.id if group is not None else None,
            )
            await session.commit()
        markup_text = (
            f"{obj.markup_percent}%" if obj.markup_percent is not None else "default"
        )
        await msg.answer(
            f"✅ <b>Маппинг сохранён</b>\n"
            f"FunPay <code>{obj.funpay_lot_id}</code> → NS#{obj.ns_service_id}\n"
            f"Markup: {markup_text}\n"
            f"Группа: {html.escape(group.name) if group is not None else '—'}\n"
            f"Label: {html.escape(obj.label or '—')}\n\n"
            f"Запусти /sync чтобы применить.",
            reply_markup=ui.single_close_kb(),
        )

    # ─────────────── helpers для команд ───────────────

    async def _render_paginated_from_cmd(
        self, msg: Message, *, kind: str, sid: str, page: int
    ) -> None:
        """Первая отрисовка из обычной команды (не из callback'а).
        Использует _send_view — старая «панель» в чате удаляется."""
        sess = self._sessions.get(sid)
        if sess is None:
            await msg.answer("Сессия не создана")
            return
        page_items, page, total_pages = paginate(sess.items, page)
        builder = {
            "ns_search": self._build_ns_search_page,
            "ns_cats": self._build_ns_cats_page,
            "ns_cat_services": self._build_ns_cat_services_page,
            "lots": self._build_lots_page,
            "mappings": self._build_mappings_page,
            "groups": self._build_groups_page,
            "group_mappings": self._build_group_mappings_page,
            "group_assign": self._build_group_assign_page,
            "orders": self._build_orders_page,
            "problems": self._build_problems_page,
        }.get(kind)
        if builder is None:
            await msg.answer(f"Неизвестный список: {kind}")
            return
        text, kb = builder(sess, sid, page_items, page, total_pages)
        await self._send_view(msg.chat.id, text, reply_markup=kb)


# ─────────────── модульные функции ───────────────


def _lot_id_of(lot) -> int | str:
    return (
        getattr(lot, "id", None)
        or getattr(lot, "lot_id", None)
        or getattr(lot, "offer_id", None)
        or "?"
    )


def _filter_services(stock: StockResponse, query: str) -> list:
    """Поиск NS-услуг: ищем по названию услуги и категории. Учитываем все слова."""
    terms = [t.strip().lower() for t in query.split() if t.strip()]
    if not terms:
        return []
    results = []
    for cat in stock.categories:
        cat_lower = (cat.category_name or "").lower()
        for svc in cat.services:
            haystack = f"{cat_lower} {svc.service_name.lower()}"
            if all(t in haystack for t in terms):
                results.append(svc)
    # Поднимем «в наличии» наверх, нулевой остаток вниз
    results.sort(key=lambda s: (s.in_stock == 0, -float(s.in_stock or 0), s.service_id))
    return results


def _format_inspect(lot_id: int, summary: dict) -> str:
    lines = [f"🔬 <b>Инспекция лота {lot_id}</b>"]
    for key, value in summary.items():
        if key == "fields.raw" and isinstance(value, dict):
            lines.append(f"<b>{html.escape(key)}</b>:")
            for k, v in value.items():
                lines.append(f"  <code>{html.escape(str(k))}</code> = {html.escape(str(v))[:120]}")
            continue
        lines.append(
            f"<b>{html.escape(key)}</b>: <code>{html.escape(str(value))[:200]}</code>"
        )
    return "\n".join(lines)
