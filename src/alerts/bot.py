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
from src.mapping.rules import compute_pricing
from src.ns import NSClient
from src.ns.models import StockResponse
from src.sync.fx import get_usd_rub_rate


SyncTrigger = Callable[[], Awaitable[dict]]
FunPayReconnect = Callable[[], Awaitable[dict]]


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
        "<b>Состояние</b>\n"
        "/status — общий обзор\n"
        "/balance — баланс NS и FunPay\n"
        "/orders — последние 10 заказов\n"
        "/sync — запустить синхронизацию\n"
        "/funpay_reconnect — переподключить FunPay\n"
        "\n"
        "<b>Каталог NS</b>\n"
        "/ns_search &lt;слово&gt; — поиск по названию услуги\n"
        "/ns_cats — список категорий NS\n"
        "\n"
        "<b>Маппинги</b>\n"
        "/lots — мои лоты на FunPay\n"
        "/mappings — текущие маппинги\n"
        "/map &lt;funpay_lot_id&gt; &lt;ns_service_id&gt; [markup%] [label]\n"
        "/unmap &lt;funpay_lot_id&gt;\n"
        "/calc &lt;funpay_lot_id&gt; — посчитать цены по маппингу\n"
        "/inspect_lot &lt;funpay_lot_id&gt; — заглянуть в LotFields\n"
        "\n"
        "<b>Прочее</b>\n"
        "/ping — проверка связи (всегда работает)\n"
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
    ) -> None:
        self._settings = settings or get_settings()
        self._sync_trigger = sync_trigger
        self._funpay_client = funpay_client
        self._funpay_reconnect = funpay_reconnect
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._task: asyncio.Task | None = None

    def update_funpay_client(self, fp: FunPayClient | None) -> None:
        """Подменить FunPay-клиент после реконнекта."""
        self._funpay_client = fp

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

        @dp.message(Command("ping"))
        async def cmd_ping(msg: Message) -> None:
            await msg.answer(
                f"🏓 pong (chat_id=<code>{msg.chat.id}</code>)\n"
                f"Если бот ответил — long-polling работает."
            )

        @dp.message(Command("version"))
        async def cmd_version(msg: Message) -> None:
            owner = self._settings.telegram_chat_id
            owner_text = (
                "<i>не задан в .env</i>" if owner is None else f"<code>{owner}</code>"
            )
            your_text = f"<code>{msg.chat.id}</code>"
            access = "✅ ты владелец" if self._is_owner(msg) else (
                "❌ ты НЕ владелец — команды кроме /start, /ping, /version, /whoami "
                "проигнорирую"
            )
            await msg.answer(
                f"🤖 NS↔FunPay Bridge\n"
                f"real_actions: <b>{self._settings.enable_real_actions}</b>\n"
                f"timezone: <b>{self._settings.timezone}</b>\n"
                f"TELEGRAM_CHAT_ID: {owner_text}\n"
                f"твой chat_id: {your_text}\n"
                f"{access}"
            )

        @dp.message(Command("help"))
        async def cmd_help(msg: Message) -> None:
            if not self._is_owner(msg):
                await msg.answer(
                    "Я отвечаю на команды только своему владельцу.\n"
                    f"Твой chat_id: <code>{msg.chat.id}</code>. "
                    f"Чтобы стать владельцем — впиши его в .env как "
                    f"<code>TELEGRAM_CHAT_ID</code> и перезапусти сервис.\n\n"
                    "Команды без авторизации: /ping, /version, /whoami, /start."
                )
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

        fp_status = "—"
        if self._funpay_client is not None:
            try:
                fp_status = (
                    f"id={self._funpay_client.account_id}, "
                    f"username={self._funpay_client.username}"
                )
            except Exception:
                fp_status = "ошибка чтения"

        text = _format_status(self._settings, last_run, balance_text)
        text += f"\n\n🔌 FunPay: <b>{fp_status}</b>"
        await msg.answer(text)

    async def _do_balance(self, msg: Message) -> None:
        lines: list[str] = []
        try:
            async with NSClient() as ns:
                bal = await ns.check_balance()
            lines.append(f"💰 NS: <b>{bal.balance}</b>")
        except Exception as exc:
            lines.append(f"💰 NS: <i>ошибка ({exc})</i>")

        if self._funpay_client is not None:
            try:
                fp_bal = await self._funpay_client.get_funpay_balance()
                if fp_bal.get("error"):
                    lines.append(f"💳 FunPay: <i>ошибка ({fp_bal['error']})</i>")
                else:
                    rub = fp_bal.get("rub") or fp_bal.get("total") or fp_bal.get("available")
                    if rub is not None:
                        lines.append(f"💳 FunPay: <b>{rub}</b>")
                    else:
                        lines.append(f"💳 FunPay (raw): <code>{fp_bal.get('raw_repr', '?')}</code>")
            except Exception as exc:
                lines.append(f"💳 FunPay: <i>{exc}</i>")
        else:
            lines.append("💳 FunPay: не подключён")

        await msg.answer("\n".join(lines))

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

    async def _fetch_stock(self) -> StockResponse:
        async with NSClient() as ns:
            return await ns.get_stock()

    async def _do_ns_search(self, msg: Message) -> None:
        text = (msg.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.answer(
                "Использование: <code>/ns_search apple</code>\n"
                "Можно несколько слов — найдёт строку с любым из них."
            )
            return
        query = parts[1].strip().lower()
        terms = [t for t in query.split() if t]

        try:
            stock = await self._fetch_stock()
        except Exception as exc:
            await msg.answer(f"NS get_stock упал: <code>{exc}</code>")
            return

        lines: list[str] = []
        total_found = 0
        for cat in stock.categories:
            cat_match = any(t in cat.category_name.lower() for t in terms)
            for svc in cat.services:
                svc_match = any(t in svc.service_name.lower() for t in terms)
                if not (cat_match or svc_match):
                    continue
                total_found += 1
                if len(lines) >= 40:
                    continue
                lines.append(
                    f"<code>{svc.service_id:5d}</code> "
                    f"{svc.service_name[:50]} | {svc.price:.4f} {svc.currency} | "
                    f"stock={svc.in_stock}"
                )

        if not lines:
            await msg.answer(
                f"По запросу «{query}» ничего не нашёл.\n"
                "Попробуй /ns_cats и посмотри по категориям."
            )
            return
        header = f"<b>Найдено: {total_found}</b>"
        if total_found > 40:
            header += " (показываю первые 40)"
        await msg.answer(header + "\n\n" + "\n".join(lines))

    async def _do_calc(self, msg: Message) -> None:
        parts = (msg.text or "").split()
        if len(parts) < 2:
            await msg.answer(
                "Использование: <code>/calc &lt;funpay_lot_id&gt;</code>\n\n"
                "Покажет какой ценой бот выставит лот: цена тебе (продавцу) "
                "и расчётная цена клиенту с учётом комиссии FunPay."
            )
            return
        try:
            funpay_lot_id = int(parts[1])
        except ValueError:
            await msg.answer("funpay_lot_id должен быть числом")
            return

        async with session_factory()() as session:
            stmt = select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
            mapping = (await session.execute(stmt)).scalar_one_or_none()
        if mapping is None:
            await msg.answer(
                f"Маппинга для лота <code>{funpay_lot_id}</code> нет.\n"
                "Сначала сделай <code>/map ...</code>"
            )
            return

        try:
            async with NSClient() as ns:
                stock = await ns.get_stock()
        except Exception as exc:
            await msg.answer(f"NS get_stock упал: <code>{exc}</code>")
            return

        svc = None
        for cat in stock.categories:
            for s in cat.services:
                if s.service_id == mapping.ns_service_id:
                    svc = s
                    break
            if svc is not None:
                break

        if svc is None:
            await msg.answer(
                f"NS service_id <code>{mapping.ns_service_id}</code> не найден в каталоге."
            )
            return

        fx_rate = await get_usd_rub_rate(self._settings)
        pricing = compute_pricing(
            ns_service=svc,
            mapping=mapping,
            settings=self._settings,
            fx_rate_usd_to_target=fx_rate,
        )

        current_seller: float | None = None
        if self._funpay_client is not None:
            try:
                summary = await self._funpay_client.get_lot_summary(funpay_lot_id)
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
        text = (
            f"📊 <b>Расчёт цены для лота {funpay_lot_id}</b>\n\n"
            f"NS: <b>{pricing.ns_price_usd:.4f}</b> USD ({svc.service_name[:50]})\n"
            f"Курс USD→{cur}: <b>{pricing.fx_rate:.4f}</b>\n"
            f"Наценка: <b>{pricing.markup_percent}%</b>\n"
            f"Комиссия FunPay (справочно): <b>{pricing.commission_percent}%</b>\n"
            f"\n"
            f"➡️ Цена продавцу (мы получим): <b>{pricing.round_price()} {cur}</b>\n"
            f"➡️ Цена клиенту (примерно): <b>{pricing.round_client_price()} {cur}</b>\n"
            f"➡️ Сток на FunPay: <b>{pricing.stock}</b>\n"
        )
        if current_seller is not None:
            text += (
                f"\nТекущая цена продавца на FunPay: <b>{current_seller}</b>\n"
                f"Обновлять? <b>{'да' if will_update else 'нет (в пределах порога)'}</b>"
            )
        await msg.answer(text)

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
            summary = await self._funpay_client.get_lot_summary(funpay_lot_id)
        except Exception as exc:
            await msg.answer(f"Ошибка: <code>{exc}</code>")
            return

        lines = [f"🔍 <b>Лот {funpay_lot_id}</b>"]
        for key, value in summary.items():
            if key == "fields.raw" and isinstance(value, dict):
                lines.append(f"<b>{key}</b>:")
                for k, v in value.items():
                    lines.append(f"  <code>{k}</code> = {v}")
                continue
            lines.append(f"<b>{key}</b>: <code>{str(value)[:200]}</code>")
        await msg.answer("\n".join(lines))

    async def _do_funpay_reconnect(self, msg: Message) -> None:
        if self._funpay_reconnect is None:
            await msg.answer("Реконнект не подключён в этой конфигурации.")
            return
        await msg.answer("⏳ Переподключаю FunPay...")
        try:
            result = await self._funpay_reconnect()
        except Exception as exc:
            await msg.answer(f"❌ Реконнект упал: <code>{exc}</code>")
            return
        if result.get("connected"):
            await msg.answer(
                f"✅ FunPay подключён\n"
                f"id: <code>{result.get('account_id')}</code>\n"
                f"username: <b>{result.get('username') or '—'}</b>"
            )
        else:
            await msg.answer(
                "❌ FunPay всё ещё недоступен. Проверь cookies (golden_key, PHPSESSID) "
                "в .env и запусти диагностику:\n"
                "<code>sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.check_funpay</code>"
            )

    async def _do_ns_cats(self, msg: Message) -> None:
        try:
            stock = await self._fetch_stock()
        except Exception as exc:
            await msg.answer(f"NS get_stock упал: <code>{exc}</code>")
            return

        if not stock.categories:
            await msg.answer("Каталог NS пустой.")
            return

        lines = [f"<b>Категории NS ({len(stock.categories)}):</b>"]
        for cat in stock.categories[:60]:
            total_stock = sum(s.in_stock for s in cat.services)
            lines.append(
                f"<code>{cat.category_id:4d}</code> {cat.category_name} — "
                f"{len(cat.services)} услуг, stock={total_stock}"
            )
        if len(stock.categories) > 60:
            lines.append(f"... и ещё {len(stock.categories) - 60}")
        await msg.answer("\n".join(lines))

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
