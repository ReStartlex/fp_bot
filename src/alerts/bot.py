"""
Интерактивный Telegram-бот на aiogram 3.x.

Команды:
    /start    — приветствие + покажет chat_id
    /help     — список команд
    /status   — статус бота, время последнего sync run, NS-баланс
    /balance  — баланс NS
    /orders   — последние 10 заказов
    /sync     — запустить sync engine прямо сейчас (в фоне)
    /whoami   — твой chat_id (на случай если забыл)

Авторизация: команды выполняются только если `chat_id == TELEGRAM_CHAT_ID` из .env.
Если `TELEGRAM_CHAT_ID` не задан — первое сообщение становится "запросом владения":
бот покажет chat_id и попросит вписать в .env.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger
from sqlalchemy import desc, select

from src.config import Settings, get_settings
from src.db.models import Mapping, Order, SyncRun
from src.db.repo import upsert_mapping
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.ns import NSClient


SyncTrigger = Callable[[], Awaitable[dict]]


def _format_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _format_order_line(o: Order) -> str:
    return (
        f"<code>{o.created_at.strftime('%m-%d %H:%M')}</code> "
        f"#{o.funpay_order_id} → {o.status} "
        f"(NS:{o.ns_custom_id or '—'})"
    )


def _format_status(settings: Settings, last_run: SyncRun | None, balance_text: str | None) -> str:
    lines = [
        "<b>NS↔FunPay Bridge — статус</b>",
        "",
        f"⚙️ Real actions: <b>{'ON' if settings.enable_real_actions else 'OFF (dry-run)'}</b>",
        f"⏱ Sync каждые: {settings.sync_interval_seconds}с",
        f"💱 Валюта FunPay: {settings.funpay_currency.value}",
        f"📈 Наценка по умолчанию: {settings.markup_percent}%",
    ]
    if balance_text is not None:
        lines.append(f"💰 Баланс NS: <b>{balance_text}</b>")
    if last_run is not None:
        lines.extend([
            "",
            "<b>Последний sync:</b>",
            f"  начат: {_format_dt(last_run.started_at)}",
            f"  завершён: {_format_dt(last_run.finished_at)}",
            f"  статус: {last_run.status}",
            f"  проверено/обновлено/пропущено: "
            f"{last_run.lots_checked}/{last_run.lots_updated}/{last_run.lots_skipped}",
        ])
        if last_run.error:
            lines.append(f"  ошибка: <code>{last_run.error[:200]}</code>")
    else:
        lines.append("")
        lines.append("Sync ещё не запускался")
    return "\n".join(lines)


class TelegramBot:
    """
    Тонкая обёртка над aiogram-ботом.

    Использование:
        bot = TelegramBot(sync_trigger=...)
        await bot.start()    # запускает long-polling в фоне
        ...
        await bot.stop()
    """

    HELP_TEXT = (
        "<b>Команды:</b>\n"
        "/status — состояние бота, последний sync, баланс\n"
        "/balance — баланс NS\n"
        "/orders — последние 10 заказов\n"
        "/sync — запустить синхронизацию прямо сейчас\n"
        "\n"
        "<b>Маппинги (NS↔FunPay):</b>\n"
        "/lots — мои лоты на FunPay (для поиска funpay_lot_id)\n"
        "/mappings — список текущих маппингов\n"
        "/map &lt;funpay_lot_id&gt; &lt;ns_service_id&gt; [markup%] — добавить/обновить\n"
        "/unmap &lt;funpay_lot_id&gt; — выключить маппинг\n"
        "\n"
        "<b>Прочее:</b>\n"
        "/whoami — твой chat_id\n"
        "/help — это сообщение"
    )

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sync_trigger: SyncTrigger | None = None,
        funpay_client: FunPayClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sync_trigger = sync_trigger
        self._funpay_client = funpay_client
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        s = self._settings
        return s.telegram_enabled and s.telegram_bot_token is not None

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

        # Дренируем старые апдейты, чтобы бот не отвечал на сообщения 2-дневной давности
        try:
            await self._bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            logger.debug(f"delete_webhook: {exc}")

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

    # ---------- Обработчики команд ----------

    def _is_owner(self, msg: Message) -> bool:
        owner = self._settings.telegram_chat_id
        if owner is None:
            return False
        return msg.chat.id == owner

    def _register_handlers(self) -> None:
        dp = self._dp
        assert dp is not None

        @dp.message(Command("start"))
        async def cmd_start(msg: Message) -> None:
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
            await msg.answer(
                f"Привет, владелец 👋\n"
                f"Я слежу за NS↔FunPay-мостом. /help — список команд."
            )

        @dp.message(Command("whoami"))
        async def cmd_whoami(msg: Message) -> None:
            await msg.answer(f"chat_id = <code>{msg.chat.id}</code>")

        @dp.message(Command("help"))
        async def cmd_help(msg: Message) -> None:
            if not self._is_owner(msg):
                return
            await msg.answer(self.HELP_TEXT)

        @dp.message(Command("status"))
        async def cmd_status(msg: Message) -> None:
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

    # ---------- Реализации команд ----------

    async def _do_status(self, msg: Message) -> None:
        async with session_factory()() as session:
            stmt = select(SyncRun).order_by(desc(SyncRun.started_at)).limit(1)
            last_run = (await session.execute(stmt)).scalar_one_or_none()

        balance_text: str | None = None
        try:
            async with NSClient() as ns:
                bal = await ns.check_balance()
                balance_text = f"{bal.balance}"
        except Exception as exc:
            balance_text = f"<i>n/a ({exc})</i>"

        await msg.answer(_format_status(self._settings, last_run, balance_text))

    async def _do_balance(self, msg: Message) -> None:
        try:
            async with NSClient() as ns:
                bal = await ns.check_balance()
            await msg.answer(f"💰 Баланс NS: <b>{bal.balance}</b>")
        except Exception as exc:
            await msg.answer(f"Не удалось получить баланс: <code>{exc}</code>")

    async def _do_orders(self, msg: Message) -> None:
        async with session_factory()() as session:
            stmt = select(Order).order_by(desc(Order.created_at)).limit(10)
            orders = (await session.execute(stmt)).scalars().all()

        if not orders:
            await msg.answer("Заказов ещё нет.")
            return

        lines = ["<b>Последние заказы:</b>"]
        for o in orders:
            lines.append(_format_order_line(o))
        await msg.answer("\n".join(lines))

    async def _do_sync(self, msg: Message) -> None:
        if self._sync_trigger is None:
            await msg.answer("Sync-движок не подключён к боту в этой конфигурации.")
            return

        await msg.answer("⏳ Запускаю sync...")
        try:
            result = await self._sync_trigger()
            await msg.answer(
                f"✅ Готово: checked={result.get('checked', 0)}, "
                f"updated={result.get('updated', 0)}, "
                f"skipped={result.get('skipped', 0)}"
            )
        except Exception as exc:
            logger.exception("Sync trigger failed")
            await msg.answer(f"❌ Sync упал: <code>{exc}</code>")

    async def _do_lots(self, msg: Message) -> None:
        if self._funpay_client is None:
            await msg.answer(
                "FunPay-клиент не подключён к боту. Скорее всего FunPay сейчас недоступен, "
                "смотри логи."
            )
            return
        try:
            lots = await self._funpay_client.get_my_lots()
        except Exception as exc:
            await msg.answer(f"FunPay get_my_lots упал: <code>{exc}</code>")
            return

        if not lots:
            await msg.answer("Лотов нет.")
            return

        lines = [f"<b>Лоты на FunPay ({len(lots)}):</b>"]
        for lot in lots[:30]:
            lot_id = (
                getattr(lot, "id", None)
                or getattr(lot, "lot_id", None)
                or getattr(lot, "offer_id", None)
                or "?"
            )
            title = (
                getattr(lot, "description", None)
                or getattr(lot, "title", None)
                or getattr(lot, "name", None)
                or ""
            )
            price = (
                getattr(lot, "price", None)
                or getattr(lot, "cost", None)
                or "—"
            )
            title_short = (str(title) or "")[:60]
            lines.append(
                f"<code>{lot_id}</code> — {price} — {title_short}"
            )
        if len(lots) > 30:
            lines.append(f"... и ещё {len(lots) - 30}")
        await msg.answer("\n".join(lines))

    async def _do_mappings(self, msg: Message) -> None:
        async with session_factory()() as session:
            stmt = select(Mapping).order_by(Mapping.funpay_lot_id)
            mappings = (await session.execute(stmt)).scalars().all()

        if not mappings:
            await msg.answer(
                "Маппингов нет.\n"
                "Используй /map &lt;funpay_lot_id&gt; &lt;ns_service_id&gt; [markup%]"
            )
            return

        lines = [f"<b>Маппинги ({len(mappings)}):</b>"]
        for m in mappings:
            status = "✅" if m.enabled else "⏸"
            markup = f"{m.markup_percent}%" if m.markup_percent is not None else "default"
            cap = m.stock_cap if m.stock_cap is not None else "default"
            label = m.label or ""
            lines.append(
                f"{status} <code>{m.funpay_lot_id}</code> → NS#{m.ns_service_id} "
                f"(markup={markup}, cap={cap}) {label}"
            )
        await msg.answer("\n".join(lines))

    async def _do_map(self, msg: Message) -> None:
        text = (msg.text or "").strip()
        parts = text.split()
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

        async with session_factory()() as session:
            obj = await upsert_mapping(
                session,
                funpay_lot_id=funpay_lot_id,
                ns_service_id=ns_service_id,
                markup_percent=markup,
                stock_cap=None,
                ns_fields_template='{"quantity":"@QUANTITY"}',
                enabled=True,
                label=label,
            )
            await session.commit()

        markup_text = f"{obj.markup_percent}%" if obj.markup_percent is not None else "default"
        await msg.answer(
            f"✅ Маппинг сохранён:\n"
            f"FunPay <code>{obj.funpay_lot_id}</code> → NS#{obj.ns_service_id}\n"
            f"Markup: {markup_text}\n"
            f"Label: {obj.label or '—'}\n\n"
            f"Запусти /sync чтобы применить."
        )

    async def _do_unmap(self, msg: Message) -> None:
        text = (msg.text or "").strip()
        parts = text.split()
        if len(parts) < 2:
            await msg.answer(
                "Использование: <code>/unmap &lt;funpay_lot_id&gt;</code>"
            )
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
        await msg.answer(
            f"⏸ Маппинг для лота <code>{funpay_lot_id}</code> выключен"
        )
