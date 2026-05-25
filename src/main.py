"""
Единый entrypoint: связывает sync engine, FunPay watcher и Telegram-бота
в одном asyncio-процессе. Запускается systemd-сервисом или вручную:

    python -m src.main

Архитектура:
    APScheduler ──> sync_once()           каждые SYNC_INTERVAL_SECONDS
    FunPay watcher ──> process_funpay_order()  на каждое NEW_ORDER
    Telegram bot ──> /status, /sync, /balance, /orders
    Heartbeat ──> Telegram info()         каждые 6 часов
    Low-balance check ──> Telegram warning() при пересечении порога
"""
from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from src.alerts.bot import TelegramBot
from src.alerts.telegram import TelegramNotifier
from src.chat.handler import ChatHandler
from src.config import get_settings
from src.db.session import close_db, init_db
from src.funpay.client import FunPayClient
from src.funpay.events import FunPayMessageEvent
from src.funpay.watcher import FunPayWatcher
from src.logging_setup import setup_logging
from src.ns import NSClient
from src.orders.reconciler import reconcile_orders_once
from src.orders.processor import FunPayOrderEvent, process_funpay_order
from src.shop.bot import ShopBot
from src.shop.catalog_sync import sync_catalog_once
from src.sync.stock_sync import sync_once


HEARTBEAT_HOURS = 6
LOW_BALANCE_CHECK_INTERVAL_MINUTES = 30
FUNPAY_RECONNECT_INTERVAL_MINUTES = 5


class App:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.scheduler: AsyncIOScheduler | None = None
        self.ns: NSClient | None = None
        self.fp: FunPayClient | None = None
        self.watcher: FunPayWatcher | None = None
        self.bot: TelegramBot | None = None
        self.shop_bot: ShopBot | None = None
        self.tg: TelegramNotifier | None = None
        self.chat_handler: ChatHandler | None = None
        self._stop_evt = asyncio.Event()
        self._last_low_balance_alert: datetime | None = None
        # Один lock на все sync-вызовы (scheduler + ручной /sync из бота)
        self._sync_lock = asyncio.Lock()
        # Счётчик последовательных падений shop catalog sync — алертим
        # только при 2+ подряд, чтобы один сетевой блип не флудил оператора.
        self._shop_catalog_consecutive_failures = 0

    # ---------- Lifecycle ----------

    async def start(self) -> None:
        setup_logging(self.settings)
        logger.info("=" * 60)
        logger.info(
            f"NS↔FunPay Bridge запускается "
            f"(real_actions={self.settings.enable_real_actions})"
        )
        logger.info(
            f"Telegram: enabled={self.settings.telegram_enabled}, "
            f"chat_id={'set' if self.settings.telegram_chat_id else 'NOT SET'}, "
            f"token={'set' if self.settings.telegram_bot_token else 'NOT SET'}"
        )
        logger.info("=" * 60)

        await init_db()

        # 1. NS-клиент: один на весь процесс, токен живёт 2 часа сам обновляется
        self.ns = NSClient()
        await self.ns.__aenter__()
        try:
            bal = await self.ns.check_balance()
            logger.info(f"NS подключён. Баланс: {bal.balance}")
        except Exception as exc:
            logger.error(f"NS check_balance упал на старте: {exc}")

        # 2. Telegram notifier (one-shot отправщик)
        self.tg = TelegramNotifier(self.settings)
        await self.tg.__aenter__()
        # Заметное сообщение о старте — раньше шёл "ℹ️ Бот запущен ✅",
        # который терялся среди ℹ️-info. Теперь с версией+временем,
        # парный к 🔴 "Бот остановлен" в stop().
        try:
            from src import _version as _v
            sha_short = getattr(_v, "SHA", "?")[:7]
            subject = (getattr(_v, "SUBJECT", "") or "").strip()
        except Exception:
            sha_short = "?"
            subject = ""
        start_msg = (
            f"🟢 <b>Бот запущен</b>\n"
            f"📦 версия: <code>{sha_short}</code>"
        )
        if subject:
            # обрезаем длинные SUBJECT (например feat(...): description...)
            short_subject = subject[:80] + ("…" if len(subject) > 80 else "")
            start_msg += f"\n📝 {short_subject}"
        await self.tg.send(start_msg)

        # 3. FunPay-клиент + watcher (с авто-повторами)
        await self._try_connect_funpay(initial=True)

        # 4. Telegram-бот с командами
        self.bot = TelegramBot(
            self.settings,
            sync_trigger=self._trigger_sync,
            funpay_client=self.fp,
            funpay_reconnect=self._funpay_reconnect,
            order_retry=self._retry_order,
        )
        await self.bot.start()

        # 4b. Shop-бот (Phase 1). Запускается только если shop_enabled=true
        # и shop_telegram_bot_token задан. Если упадёт на старте — не валим
        # bridge-бот, просто отключаем shop и шлём алерт владельцу.
        try:
            self.shop_bot = ShopBot(
                self.settings,
                owner_notify=self._notify_owner_safe,
            )
            await self.shop_bot.start()
        except Exception as exc:
            logger.exception(f"Shop-бот не стартовал: {exc}")
            if self.tg is not None:
                short = f"{type(exc).__name__}: {str(exc)[:160]}"
                await self.tg.error(
                    f"Shop-бот не стартовал: <code>{short}</code>. "
                    "Bridge продолжит работать, shop останется выключенным."
                )
            self.shop_bot = None

        # 5. Планировщик: sync + heartbeat + low-balance
        self.scheduler = AsyncIOScheduler(timezone=self.settings.timezone)
        self.scheduler.add_job(
            self._safe_sync,
            "interval",
            seconds=self.settings.sync_interval_seconds,
            id="sync",
            next_run_time=datetime.now(),
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._heartbeat,
            "interval",
            hours=HEARTBEAT_HOURS,
            id="heartbeat",
        )
        self.scheduler.add_job(
            self._check_low_balance,
            "interval",
            minutes=LOW_BALANCE_CHECK_INTERVAL_MINUTES,
            id="low_balance",
            next_run_time=datetime.now() + timedelta(seconds=10),
        )
        self.scheduler.add_job(
            self._funpay_reconnect_if_needed,
            "interval",
            minutes=FUNPAY_RECONNECT_INTERVAL_MINUTES,
            id="funpay_reconnect",
            max_instances=1,
            coalesce=True,
        )
        if self.settings.order_reconcile_enabled:
            self.scheduler.add_job(
                self._safe_reconcile_orders,
                "interval",
                seconds=self.settings.order_reconcile_interval_seconds,
                id="order_reconciler",
                next_run_time=datetime.now() + timedelta(seconds=30),
                max_instances=1,
                coalesce=True,
            )
        if self.settings.new_lots_notify_enabled:
            self.scheduler.add_job(
                self._safe_discover_new_lots,
                "interval",
                seconds=self.settings.new_lots_check_interval_seconds,
                id="new_lots",
                next_run_time=datetime.now() + timedelta(seconds=15),
                max_instances=1,
                coalesce=True,
            )
        # Shop-каталог: тянем NS.get_stock и обновляем shop_catalog_cache.
        # Запускаем даже если shop_enabled=false и shop-бот выключен —
        # пусть кеш накапливается заранее, чтобы при включении был мгновенный
        # UX. NSClient у нас один на процесс, лишних подключений нет.
        if self.shop_bot is not None and self.settings.shop_enabled:
            self.scheduler.add_job(
                self._safe_shop_catalog_sync,
                "interval",
                seconds=self.settings.shop_catalog_refresh_seconds,
                id="shop_catalog_sync",
                # Чуть позже старта, чтобы дать NS прогреться и
                # bridge-sync_once отработал свой первый цикл первым.
                next_run_time=datetime.now() + timedelta(seconds=20),
                max_instances=1,
                coalesce=True,
            )
        self.scheduler.start()
        logger.info(
            f"Планировщик запущен: sync каждые "
            f"{self.settings.sync_interval_seconds}с, "
            f"discovery новых лотов каждые "
            f"{self.settings.new_lots_check_interval_seconds}с, "
            f"heartbeat каждые {HEARTBEAT_HOURS}ч"
        )

    async def stop(self) -> None:
        logger.info("Останавливаю бота...")
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
        if self.watcher is not None:
            self.watcher.stop()
        if self.bot is not None:
            await self.bot.stop()
        if self.shop_bot is not None:
            with suppress(Exception):
                await self.shop_bot.stop()
        if self.fp is not None:
            await self.fp.__aexit__(None, None, None)
        if self.tg is not None:
            with suppress(Exception):
                await self.tg.send("🔴 <b>Бот остановлен</b>")
            await self.tg.__aexit__(None, None, None)
        if self.ns is not None:
            await self.ns.__aexit__(None, None, None)
        await close_db()
        logger.info("Чисто. Выхожу.")

    async def run_forever(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()

        def _handle_signal() -> None:
            logger.info("Получен сигнал, начинаю shutdown")
            self._stop_evt.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                # Windows: SIGTERM не поддерживается
                loop.add_signal_handler(sig, _handle_signal)

        await self._stop_evt.wait()
        await self.stop()

    # ---------- FunPay (re)connect ----------

    async def _try_connect_funpay(self, *, initial: bool = False) -> bool:
        """
        Пытается поднять FunPay-клиент и watcher.
        Возвращает True если подключение успешно.
        """
        if self.fp is not None and self.watcher is not None:
            return True

        fp = FunPayClient(self.settings)
        try:
            await fp.__aenter__()
            await fp.connect()
        except Exception as exc:
            await fp.__aexit__(None, None, None)
            if initial:
                logger.warning(
                    f"FunPay подключение упало: {exc}. "
                    "Sync и Telegram-бот продолжат работать; "
                    f"авто-повтор через {FUNPAY_RECONNECT_INTERVAL_MINUTES} мин."
                )
                if self.tg is not None:
                    await self.tg.warning(
                        f"FunPay не подключился: <code>{exc}</code>. "
                        f"Авто-повтор каждые {FUNPAY_RECONNECT_INTERVAL_MINUTES} мин. "
                        "Если хочешь подтолкнуть — /funpay_reconnect."
                    )
            else:
                logger.debug(f"FunPay reconnect упал снова: {exc}")
            return False

        self.fp = fp
        self.chat_handler = ChatHandler(self.fp, telegram=self.tg, settings=self.settings)
        self.watcher = FunPayWatcher(
            self.fp,
            on_new_order=self._on_new_order,
            on_new_message=self._on_new_message,
            poll_interval_seconds=self.settings.funpay_poll_interval_seconds,
            listen_enabled=self.settings.funpay_listen_enabled,
            active_fetch_limit=self.settings.funpay_active_chats_poll_limit,
        )
        try:
            self.watcher.start()
        except Exception as exc:
            logger.exception(f"FunPay watcher не запустился: {exc}")
            self.watcher = None

        # Перепривяжем бота к новому FunPay-клиенту
        if self.bot is not None:
            self.bot.update_funpay_client(self.fp)

        msg = (
            f"FunPay подключился: id={self.fp.account_id}, "
            f"username={self.fp.username}, balance={self.fp.balance}"
        )
        logger.success(msg)
        if not initial and self.tg is not None:
            await self.tg.info(f"✅ {msg}")
        return True

    async def _funpay_reconnect_if_needed(self) -> None:
        if self.fp is not None and self.watcher is not None:
            return
        ok = await self._try_connect_funpay(initial=False)
        if not ok:
            logger.debug("FunPay scheduled reconnect: всё ещё недоступно")

    async def _funpay_reconnect(self) -> dict:
        """Ручной триггер из Telegram-бота. Сбрасывает текущее подключение и пробует заново."""
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        if self.fp is not None:
            await self.fp.__aexit__(None, None, None)
            self.fp = None
        if self.bot is not None:
            self.bot.update_funpay_client(None)

        ok = await self._try_connect_funpay(initial=False)
        return {
            "connected": ok,
            "account_id": self.fp.account_id if self.fp else None,
            "username": self.fp.username if self.fp else None,
        }

    # ---------- Sync ----------

    async def _safe_sync(self) -> None:
        async with self._sync_lock:
            try:
                await sync_once(funpay_client=self.fp, ns_client=self.ns)
            except Exception as exc:
                logger.exception(f"Sync упал: {exc}")
                if self.tg is not None:
                    # Детали ошибки только в лог; в Telegram — сжатое сообщение
                    short = str(exc)[:200]
                    await self.tg.error(f"Sync run упал: <code>{short}</code>")

    async def _trigger_sync(self) -> dict:
        """Вручную из Telegram-бота. Не запустится параллельно с scheduler-sync."""
        async with self._sync_lock:
            return await sync_once(funpay_client=self.fp, ns_client=self.ns)

    async def _safe_discover_new_lots(self) -> None:
        """Периодический поиск новых FunPay-лотов с алертом в Telegram."""
        try:
            from src.sync.new_lots import discover_new_lots
            await discover_new_lots(
                self.fp, self.tg, ns_client=self.ns, settings=self.settings
            )
        except Exception as exc:
            logger.exception(f"discover_new_lots упал: {exc}")

    async def _safe_reconcile_orders(self) -> None:
        try:
            result = await reconcile_orders_once(
                settings=self.settings,
                ns_client=self.ns,
                funpay_client=self.fp,
                telegram=self.tg,
            )
            if result.get("checked", 0):
                logger.info(f"order reconciler: {result}")
        except Exception as exc:
            logger.exception(f"order reconciler упал: {exc}")

    # ---------- FunPay events ----------

    async def _on_new_message(self, event: FunPayMessageEvent) -> None:
        if self.chat_handler is None:
            return
        try:
            await self.chat_handler.on_message(event)
        except Exception as exc:
            logger.exception(f"ChatHandler упал на сообщении {event!r}: {exc}")

    async def _on_new_order(self, event: FunPayOrderEvent) -> None:
        try:
            result = await process_funpay_order(
                event,
                settings=self.settings,
                ns_client=self.ns,
                funpay_client=self.fp,
                telegram=self.tg,
            )
            logger.info(f"Заказ {event.funpay_order_id} обработан: {result}")
        except Exception as exc:
            logger.exception(f"Order processor упал для {event.funpay_order_id}: {exc}")
            if self.tg is not None:
                # Полный traceback — в файловый лог (см. journalctl/logs/),
                # в Telegram отдаём короткое сообщение и тип исключения.
                short = f"{type(exc).__name__}: {str(exc)[:160]}"
                await self.tg.error(
                    f"Order processor exception для "
                    f"<code>{event.funpay_order_id}</code>: <code>{short}</code>\n"
                    "Полный traceback — в логах сервера."
                )

    async def _retry_order(self, funpay_order_id: str) -> dict:
        from src.db.repo import find_order_by_funpay_id
        from src.db.session import session_factory

        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, funpay_order_id)
            if order is None:
                return {"status": "not_found", "reason": "order not found"}
            if order.status not in ("pins_ready", "manual_hold"):
                return {
                    "status": "skipped",
                    "reason": (
                        "retry поддержан только для pins_ready/manual_hold, "
                        f"сейчас {order.status}"
                    ),
                }
            event = FunPayOrderEvent(
                funpay_order_id=order.funpay_order_id,
                funpay_lot_id=order.funpay_lot_id,
                buyer_username=order.buyer_username,
                buyer_user_id=order.buyer_user_id,
                chat_id=order.chat_id,
                quantity=order.quantity,
                funpay_price_rub=order.funpay_price_rub,
                description=order.description,
            )

        return await process_funpay_order(
            event,
            settings=self.settings,
            ns_client=self.ns,
            funpay_client=self.fp,
            telegram=self.tg,
            force_delivery=True,
        )

    # ---------- Shop helpers ----------

    async def _safe_shop_catalog_sync(self) -> None:
        """
        Тонкая обёртка над sync_catalog_once: ловит исключения, чтобы
        APScheduler не отвалился, и в Telegram владельцу не валит спам
        (каталог обновляется часто; алертим только на устойчивые сбои —
        2+ failed подряд).
        """
        if self.ns is None:
            return
        try:
            result = await sync_catalog_once(
                ns_client=self.ns, settings=self.settings
            )
        except Exception as exc:
            logger.exception(f"shop catalog sync упал: {exc}")
            self._shop_catalog_consecutive_failures += 1
            if (
                self._shop_catalog_consecutive_failures == 2
                and self.tg is not None
            ):
                short = f"{type(exc).__name__}: {str(exc)[:160]}"
                await self.tg.error(
                    f"Shop catalog sync падает: <code>{short}</code>. "
                    "Каталог покупателям не обновляется."
                )
            return
        if result.get("status") == "ok":
            if self._shop_catalog_consecutive_failures >= 2 and self.tg is not None:
                await self.tg.info(
                    "✅ Shop catalog sync восстановился"
                )
            self._shop_catalog_consecutive_failures = 0
        elif result.get("status") == "failed":
            self._shop_catalog_consecutive_failures += 1

    async def _notify_owner_safe(self, text: str) -> None:
        """
        Уведомление владельца из shop-бота. Никогда не бросает исключения —
        shop-бот не должен ронять основной процесс, если Telegram временно
        недоступен.
        """
        if self.tg is None:
            return
        try:
            await self.tg.send(text)
        except Exception as exc:
            logger.debug(f"_notify_owner_safe: {exc}")

    # ---------- Health ----------

    async def _heartbeat(self) -> None:
        if self.tg is None or not self.tg.enabled:
            return
        try:
            async with NSClient() as ns:
                bal = await ns.check_balance()
            balance_text = bal.balance
        except Exception as exc:
            balance_text = f"<i>n/a ({exc})</i>"
        await self.tg.info(
            f"⏰ Heartbeat. Жив. Баланс NS: <b>{balance_text}</b>"
        )

    async def _check_low_balance(self) -> None:
        if self.ns is None or self.tg is None:
            return
        threshold = self.settings.ns_low_balance_threshold
        try:
            bal = await self.ns.check_balance()
        except Exception as exc:
            logger.debug(f"low_balance check: NS недоступен: {exc}")
            return

        try:
            current = float(bal.balance)
        except (TypeError, ValueError):
            return

        if current >= threshold:
            self._last_low_balance_alert = None
            return

        # Не спамим: алерт не чаще раза в 6 часов
        now = datetime.now()
        if (
            self._last_low_balance_alert is not None
            and now - self._last_low_balance_alert < timedelta(hours=6)
        ):
            return
        self._last_low_balance_alert = now
        await self.tg.low_balance(current=current, threshold=threshold)


async def _main_async() -> int:
    app = App()
    try:
        await app.run_forever()
    except Exception as exc:
        logger.exception(f"Fatal: {exc}")
        return 1
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
