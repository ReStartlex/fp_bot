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
from src.config import get_settings
from src.db.session import close_db, init_db
from src.funpay.client import FunPayClient
from src.funpay.watcher import FunPayWatcher
from src.logging_setup import setup_logging
from src.ns import NSClient
from src.orders.processor import FunPayOrderEvent, process_funpay_order
from src.sync.stock_sync import sync_once


HEARTBEAT_HOURS = 6
LOW_BALANCE_CHECK_INTERVAL_MINUTES = 30


class App:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.scheduler: AsyncIOScheduler | None = None
        self.ns: NSClient | None = None
        self.fp: FunPayClient | None = None
        self.watcher: FunPayWatcher | None = None
        self.bot: TelegramBot | None = None
        self.tg: TelegramNotifier | None = None
        self._stop_evt = asyncio.Event()
        self._last_low_balance_alert: datetime | None = None

    # ---------- Lifecycle ----------

    async def start(self) -> None:
        setup_logging(self.settings)
        logger.info("=" * 60)
        logger.info(
            f"NS↔FunPay Bridge запускается "
            f"(real_actions={self.settings.enable_real_actions})"
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
        await self.tg.info("Бот запущен ✅")

        # 3. FunPay-клиент + watcher
        self.fp = FunPayClient(self.settings)
        await self.fp.__aenter__()
        try:
            await self.fp.connect()
        except Exception as exc:
            logger.warning(
                f"FunPay подключение упало: {exc}. "
                "Watcher не будет запущен, но sync и Telegram-бот будут работать."
            )
            await self.tg.warning(
                f"FunPay не подключился: <code>{exc}</code>. "
                "Sync engine работает, watcher выключен."
            )
            self.fp = None  # помечаем что FunPay недоступен

        if self.fp is not None:
            self.watcher = FunPayWatcher(self.fp, self._on_new_order)
            try:
                self.watcher.start()
            except Exception as exc:
                logger.exception(f"FunPay watcher не запустился: {exc}")
                self.watcher = None

        # 4. Telegram-бот с командами
        self.bot = TelegramBot(
            self.settings,
            sync_trigger=self._trigger_sync,
            funpay_client=self.fp,
        )
        await self.bot.start()

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
        self.scheduler.start()
        logger.info(
            f"Планировщик запущен: sync каждые {self.settings.sync_interval_seconds}с, "
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
        if self.fp is not None:
            await self.fp.__aexit__(None, None, None)
        if self.tg is not None:
            with suppress(Exception):
                await self.tg.info("Бот остановлен ⏹")
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

    # ---------- Sync ----------

    async def _safe_sync(self) -> None:
        try:
            await sync_once(funpay_client=self.fp, ns_client=self.ns)
        except Exception as exc:
            logger.exception(f"Sync упал: {exc}")
            if self.tg is not None:
                await self.tg.error(f"Sync run упал: <code>{exc}</code>")

    async def _trigger_sync(self) -> dict:
        """Вручную из Telegram-бота."""
        return await sync_once(funpay_client=self.fp, ns_client=self.ns)

    # ---------- FunPay events ----------

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
                await self.tg.error(
                    f"Order processor exception для "
                    f"<code>{event.funpay_order_id}</code>: <code>{exc}</code>"
                )

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
