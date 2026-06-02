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
from src.orders.discovery import discover_new_orders_once
from src.orders.reconciler import reconcile_orders_once
from src.orders.processor import FunPayOrderEvent, process_funpay_order
from src.shop.bot import ShopBot
from src.shop.catalog_sync import sync_catalog_once
from src.shop.delivery import (
    deliver_shop_order_once,
    poll_shop_deliveries_once,
)
from src.shop.payments.poller import poll_cryptobot_once
from src.sync.stock_sync import sync_once
from src.sync.zombie_reaper import reap_zombie_lots_once


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
        # Аналогичный счётчик для CryptoBot polling. Порог 3 (а не 2),
        # потому что getInvoices иногда тротлится — лучше дать ему
        # один лишний retry перед алертом.
        self._cryptobot_poll_consecutive_failures = 0
        # Счётчик подряд-падений zombie reaper'а. При 3+ подряд (~30 мин)
        # — алертим: значит FunPay недоступен или маппинги БД сломаны.
        self._zombie_reaper_consecutive_failures = 0
        # Счётчик последовательных провалов NS health watchdog.
        # Перешёл за threshold → летит «🚨 NS лежит». См. _ns_health_check.
        self._ns_health_consecutive_failures = 0
        # Счётчик подряд циклов sync с exhausted>0. На 3+ подряд — алертим
        # владельцу, чтобы понять, что лоты в этом окне не апдейтятся.
        self._sync_exhausted_streak = 0

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
            # Передаём долгоживущий NSClient: бот переиспользует токен,
            # который и так держится sync'ом, без повторного login'а.
            ns_client=self.ns,
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
            # Sprint 5: inline-доставка сразу после buy_confirm. Если падает —
            # APScheduler-воркер всё равно подберёт заказ на следующем тике.
            self.shop_bot._delivery_runner = self._shop_inline_deliver
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
        # Sprint 3: CryptoBot polling — паылем getInvoices(status=paid) и
        # идемпотентно начисляем юзерам пополнения. Только если токен задан;
        # иначе функционал «🪙 CryptoBot» остаётся stub-ом.
        if (
            self.shop_bot is not None
            and self.settings.shop_enabled
            and self.settings.cryptobot_api_token is not None
        ):
            self.scheduler.add_job(
                self._safe_cryptobot_poll,
                "interval",
                seconds=self.settings.cryptobot_polling_seconds,
                id="cryptobot_poll",
                # Сдвигаем фазу относительно sync_once (старт+0с,
                # каждые 30с) и catalog_sync (старт+20с): первый прогон
                # на +45с, дальше с шагом 30с — это даёт расхождение
                # с sync_once в 15с (никогда не совпадают по фазе).
                # WAL и так покрывает конкуренцию, но «гигиена» не
                # лишняя — меньше шансов на busy_timeout retry.
                next_run_time=datetime.now() + timedelta(seconds=45),
                max_instances=1,
                coalesce=True,
            )
        # Sprint 5: Shop delivery worker — обрабатывает paid/delivering shop_orders.
        # Это полный аналог NS-пайплайна orders/processor.py, но для shop-заказов
        # (покупки покупателей через NeuroDrop, оплаченные с внутреннего баланса).
        # Воркер:
        #   * Берёт N paid/delivering заказов
        #   * Для каждого вызывает NS create_order + pay_order + wait_completion
        #   * Помечает delivered с pins, шлёт юзеру; или failed → refund
        #   * Cashback инвайтеру при успешной доставке (1% по умолчанию)
        if self.shop_bot is not None and self.settings.shop_enabled:
            self.scheduler.add_job(
                self._safe_shop_delivery_poll,
                "interval",
                seconds=self.settings.shop_delivery_poll_seconds,
                id="shop_delivery_poll",
                next_run_time=datetime.now() + timedelta(seconds=60),
                max_instances=1,
                coalesce=True,
            )

        # Zombie-lot reaper: устраняет half-disabled state после failed-заказов.
        # Сценарий: _emergency_disable_lot отключил mapping, но save_lot на
        # FunPay упал — лот продолжает продаваться со старым stock'ом.
        # Reaper находит такие лоты и пытается deactivate их снова.
        if self.settings.zombie_lot_reaper_enabled:
            self.scheduler.add_job(
                self._safe_zombie_reap,
                "interval",
                seconds=self.settings.zombie_lot_reaper_interval_seconds,
                id="zombie_reaper",
                # Phase shift: +90с после старта. Reaper не должен пересекаться
                # с большими циклами sync_once (когда TTL diff-cache у всех
                # истекает одновременно и r429 spike). Дольше — потому что
                # этот job не критичен по latency, лот в half-disabled state
                # может подождать ещё 10 минут без катастрофы.
                next_run_time=datetime.now() + timedelta(seconds=90),
                max_instances=1,
                coalesce=True,
            )

        # Order discovery: 3-й канал доставки заказов (listen-loop выключен,
        # poll-loop не обрабатывает ORDER-события). Раз в N секунд берёт
        # paid-заказы со страницы /orders/trade и для отсутствующих в БД
        # запускает processor. См. src/orders/discovery.py.
        if self.settings.funpay_order_discovery_enabled:
            self.scheduler.add_job(
                self._safe_order_discovery,
                "interval",
                seconds=self.settings.funpay_order_discovery_interval_seconds,
                id="order_discovery",
                # Запускаем чуть позже стандартного sync — пусть NS-токен и
                # FunPay-сессия успеют прогреться при старте процесса. 25с
                # хватает с запасом и не пересекается с sync_once (старт+0с).
                next_run_time=datetime.now() + timedelta(seconds=25),
                max_instances=1,
                coalesce=True,
            )

        # NS health watchdog: периодический пинг балансом + алерт в
        # Telegram при длительной недоступности NS. См. _ns_health_check.
        if self.settings.ns_health_watchdog_enabled:
            self.scheduler.add_job(
                self._ns_health_check,
                "interval",
                seconds=self.settings.ns_health_watchdog_interval_seconds,
                id="ns_health_watchdog",
                # Сдвиг +40с относительно старта, чтобы не пересекаться с
                # первым sync_once (тоже тыкает NS) и order_discovery (+25с).
                next_run_time=datetime.now() + timedelta(seconds=40),
                max_instances=1,
                coalesce=True,
            )

        self.scheduler.start()
        # Лог-сводка реально активных job'ов — критично для диагностики
        # «job точно запущен или нет?» по journalctl. Если в .env что-то
        # не настроено (например, shop_telegram_bot_token), соответствующий
        # job не зарегистрируется, и его не будет в этой строке.
        active_jobs = [j.id for j in self.scheduler.get_jobs()]
        logger.info(
            f"Планировщик запущен: sync каждые "
            f"{self.settings.sync_interval_seconds}с, "
            f"discovery новых лотов каждые "
            f"{self.settings.new_lots_check_interval_seconds}с, "
            f"heartbeat каждые {HEARTBEAT_HOURS}ч; "
            f"всего job'ов: {len(active_jobs)} ({', '.join(active_jobs)})"
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
            # Дефолт 1024 был мал для активного аккаунта: при ~30 чатах
            # × ~10 сообщений в час и spike-нагрузках можно было словить
            # повторный дисптач старого сообщения после ротации deque.
            # 4096 запасом покрывает сутки активной торговли.
            dedup_cache_size=self.settings.funpay_watcher_dedup_cache_size,
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
                result = await sync_once(funpay_client=self.fp, ns_client=self.ns)
            except Exception as exc:
                logger.exception(f"Sync упал: {exc}")
                if self.tg is not None:
                    short = str(exc)[:200]
                    await self.tg.error(f"Sync run упал: <code>{short}</code>")
                return

            # Visibility: если retry-логика FunPay-клиента не справилась
            # (429 повторялся пока budget не кончился), часть лотов в этом
            # цикле НЕ обновилась. Один такой случай — это «плохая минута»,
            # 3 подряд — это уже инцидент: либо FunPay поджимает наши
            # лимиты, либо мы превысили частоту save_lot. Алертим только
            # на устойчивые серии, чтобы не флудить.
            exhausted = int((result.get("http") or {}).get("exhausted", 0))
            if exhausted > 0:
                self._sync_exhausted_streak += 1
                if (
                    self._sync_exhausted_streak == 3
                    and self.tg is not None
                ):
                    try:
                        await self.tg.warning(
                            f"⚠ <b>Sync: rate-limit exhausted ×3 подряд</b>\n"
                            f"Последний цикл: exhausted=<b>{exhausted}</b> лотов.\n"
                            f"FunPay часто отдаёт 429, ретраи иссякли — "
                            f"эти лоты в данном цикле НЕ обновились "
                            f"(подберёт след. тик).\n"
                            f"Если повторяется регулярно: подумай о "
                            f"<code>funpay_save_lot_min_interval_ms</code> "
                            f"или о снижении частоты sync."
                        )
                    except Exception as exc:
                        logger.warning(f"sync exhausted alert не доставлен: {exc}")
            else:
                if (
                    self._sync_exhausted_streak >= 3
                    and self.tg is not None
                ):
                    try:
                        await self.tg.info(
                            "✅ Sync: rate-limit восстановился (exhausted=0)."
                        )
                    except Exception as exc:
                        logger.warning(f"sync exhausted recover alert не доставлен: {exc}")
                self._sync_exhausted_streak = 0

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

    async def _safe_order_discovery(self) -> None:
        """3-й канал доставки заказов: см. src/orders/discovery.py."""
        if self.fp is None:
            logger.debug("order discovery: FunPay-клиент не подключён, skip")
            return
        try:
            result = await discover_new_orders_once(
                funpay_client=self.fp,
                ns_client=self.ns,
                settings=self.settings,
                telegram=self.tg,
            )
        except Exception as exc:
            logger.exception(f"order discovery упал: {exc}")
            return
        # Логируем только если что-то реально произошло — чтобы не
        # засорять journalctl каждые 60с пустыми «fetched=0» строками.
        if result.fetched and (result.dispatched or result.errors):
            logger.info(f"order discovery: {result.as_dict()}")
        elif result.dispatched or result.errors:
            logger.info(f"order discovery: {result.as_dict()}")

    async def _ns_health_check(self) -> None:
        """Лёгкий ping NS через check_balance + алерт при N подряд провалах.

        Если есть длительная недоступность NS, владелец узнаёт об этом
        быстро (а не через 6-часовой heartbeat) и может вручную выключить
        все лоты кнопкой в Telegram-боте.

        Возвращающие алерты («✅ NS жив») шлём только если до этого был
        активный «🚨» — иначе нет смысла информировать о том, что и так
        работает.
        """
        if self.ns is None:
            return
        timeout = float(self.settings.ns_health_watchdog_check_timeout_seconds)
        threshold = int(self.settings.ns_health_watchdog_alert_after_failures)

        try:
            await asyncio.wait_for(self.ns.check_balance(), timeout=timeout)
            ok = True
            err_message = ""
        except Exception as exc:
            ok = False
            err_message = f"{type(exc).__name__}: {str(exc)[:160]}"

        if ok:
            if self._ns_health_consecutive_failures >= threshold and self.tg is not None:
                downtime_minutes = max(
                    1,
                    self._ns_health_consecutive_failures
                    * self.settings.ns_health_watchdog_interval_seconds
                    // 60,
                )
                try:
                    await self.tg.info(
                        f"✅ <b>NS.gifts восстановился</b> "
                        f"(простой ~{downtime_minutes} мин). "
                        f"Можно включать лоты обратно: меню → "
                        f"«🟢 Включить все лоты»."
                    )
                except Exception as send_exc:
                    logger.warning(
                        f"ns watchdog: alert recover не доставлен: {send_exc}"
                    )
            self._ns_health_consecutive_failures = 0
            return

        self._ns_health_consecutive_failures += 1
        logger.warning(
            f"ns watchdog: NS недоступен "
            f"({self._ns_health_consecutive_failures}/{threshold}): {err_message}"
        )

        # Первый алерт — ровно на пороге; дальше — раз в hour-loop по
        # модулю, чтобы не флудить, но напоминать раз в N тиков.
        # 12 тиков ≈ 18 минут при interval=90с — терпимо.
        should_alert = (
            self._ns_health_consecutive_failures == threshold
            or (
                self._ns_health_consecutive_failures > threshold
                and self._ns_health_consecutive_failures % 12 == 0
            )
        )
        if not should_alert or self.tg is None:
            return

        downtime_minutes = max(
            1,
            self._ns_health_consecutive_failures
            * self.settings.ns_health_watchdog_interval_seconds
            // 60,
        )
        try:
            await self.tg.error(
                f"🚨 <b>NS.gifts недоступен</b> уже ~{downtime_minutes} мин.\n"
                f"Последняя ошибка: <code>{err_message}</code>\n\n"
                f"Пока NS лежит, заказы не выдаются. "
                f"Рекомендую выключить лоты: меню → «🔴 Выключить все лоты». "
                f"После восстановления приду с «✅ NS жив»."
            )
        except Exception as send_exc:
            logger.warning(f"ns watchdog: alert fail не доставлен: {send_exc}")

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

    async def _safe_cryptobot_poll(self) -> None:
        """
        Идемпотентный прогон CryptoBot polling-воркера.

        Никогда не падает наружу. На устойчивые сбои (3+ подряд) — алертим
        владельцу: значит токен невалиден или API лежит, новые пополнения
        не зачисляются.
        """
        try:
            result = await poll_cryptobot_once(
                settings=self.settings,
                notifier=self._notify_buyer_cryptobot_paid,
            )
        except Exception as exc:
            logger.exception(f"cryptobot poll упал: {exc}")
            self._cryptobot_poll_consecutive_failures += 1
            if (
                self._cryptobot_poll_consecutive_failures == 3
                and self.tg is not None
            ):
                short = f"{type(exc).__name__}: {str(exc)[:160]}"
                await self.tg.error(
                    f"🪙 CryptoBot poll падает: <code>{short}</code>. "
                    "Новые пополнения не зачисляются."
                )
            return
        if result.errors > 0:
            self._cryptobot_poll_consecutive_failures += 1
        else:
            if (
                self._cryptobot_poll_consecutive_failures >= 3
                and self.tg is not None
            ):
                await self.tg.info("✅ CryptoBot poll восстановился")
            self._cryptobot_poll_consecutive_failures = 0

    async def _safe_zombie_reap(self) -> None:
        """
        Безопасный прогон zombie-reaper'а. Никогда не падает наружу.

        На устойчивые сбои (3+ подряд = ~30 минут с дефолтным интервалом)
        — алертим владельцу. Чаще всего это значит, что FunPay-сессия
        невалидна, и тогда вообще ничего не работает.
        """
        if self.fp is None:
            logger.debug("zombie reaper: FunPay-клиент не подключён, skip")
            return

        try:
            result = await reap_zombie_lots_once(
                funpay_client=self.fp,
                settings=self.settings,
                notify_owner=self._notify_owner_safe,
            )
        except Exception as exc:
            logger.exception(f"zombie reaper упал: {exc}")
            self._zombie_reaper_consecutive_failures += 1
            if (
                self._zombie_reaper_consecutive_failures == 3
                and self.tg is not None
            ):
                short = f"{type(exc).__name__}: {str(exc)[:160]}"
                await self.tg.error(
                    f"🧟 Zombie reaper падает: <code>{short}</code>. "
                    "Half-disabled лоты не вычищаются."
                )
            return

        # Логируем результат всегда: heartbeat zombie-reaper'а полезен
        # для подтверждения что job живой (как cryptobot poll log).
        logger.info(
            f"zombie reaper: checked={result.checked} "
            f"already_dead={result.already_dead} "
            f"deactivated={result.deactivated} "
            f"errors={result.errors}"
        )

        if result.errors > 0:
            self._zombie_reaper_consecutive_failures += 1
        else:
            if (
                self._zombie_reaper_consecutive_failures >= 3
                and self.tg is not None
            ):
                await self.tg.info("✅ Zombie reaper восстановился")
            self._zombie_reaper_consecutive_failures = 0

    async def _safe_shop_delivery_poll(self) -> None:
        """
        Sprint 5: периодический воркер доставки shop-заказов.

        Никогда не падает наружу. Берёт до N paid/delivering заказов
        и пытается завершить каждый через NS-пайплайн.

        Inline-runner (`_shop_inline_deliver`) обрабатывает заказы СРАЗУ
        после buy_confirm — этот воркер на 60s интервале служит safety net
        для:
          * Случая когда inline-таска упала / была отменена;
          * Заказов которые остались в DELIVERING после timeout
            (wait_completion вернул NSOrderTimeoutError, retry на след тике);
          * Восстановления после рестарта процесса (paid orders в БД
            остаются неоплаченными в NS).
        """
        if self.ns is None or self.shop_bot is None:
            return
        try:
            metrics = await poll_shop_deliveries_once(
                ns=self.ns,
                settings=self.settings,
                notify_buyer=self._shop_notify_buyer,
                notify_owner=self._notify_owner_safe,
                max_per_run=self.settings.shop_delivery_max_per_run,
            )
        except Exception as exc:
            logger.exception(f"shop delivery poll упал: {exc}")
            return
        if metrics["checked"] > 0:
            logger.info(
                f"shop delivery poll: "
                f"checked={metrics['checked']} "
                f"delivered={metrics['delivered']} "
                f"failed={metrics['failed']} "
                f"pending={metrics['pending']}"
            )

    async def _shop_inline_deliver(
        self, order_id: int, tg_user_id: int,
    ) -> None:
        """
        Inline-runner: вызывается ShopBot'ом сразу после успешного
        attempt_checkout_via_balance, чтобы юзер не ждал 60s до следующего
        тика воркера.

        Изоляция от UI:
          * Никогда не бросает наружу — все ошибки логируются и (если
            нужно) превратятся в notify_buyer/refund внутри
            deliver_shop_order_once.
          * Получает order_id, а не сам ShopOrder — на момент вызова
            заказ может быть в любом состоянии, deliver_once сам решит.
        """
        if self.ns is None:
            logger.warning("inline deliver: NS не подключён")
            return
        try:
            await deliver_shop_order_once(
                order_id,
                ns=self.ns,
                settings=self.settings,
                notify_buyer=self._shop_notify_buyer,
                notify_owner=self._notify_owner_safe,
            )
        except Exception as exc:
            logger.exception(
                f"inline deliver order {order_id} упал (worker подберёт): {exc}"
            )

    async def _shop_notify_buyer(self, user_id: int, text: str) -> None:
        """
        Отправляет покупателю сообщение через shop-бота, ID = shop_users.id
        (НЕ Telegram user_id!). Маппинг shop_users.id → telegram_user_id
        выполняется здесь.
        """
        if self.shop_bot is None:
            return
        # Локальные импорты — session_factory / ShopUser нужны только здесь,
        # глобально не таскаем чтобы не утяжелять header модуля.
        from sqlalchemy import select
        from src.db.session import session_factory
        from src.db.models import ShopUser
        try:
            async with session_factory()() as session:
                row = await session.execute(
                    select(ShopUser).where(ShopUser.id == user_id)
                )
                user = row.scalar_one_or_none()
            if user is None:
                logger.warning(f"shop notify buyer: user {user_id} not found")
                return
            await self.shop_bot.send_message_to_user(user.telegram_user_id, text)
        except Exception as exc:
            logger.warning(f"shop notify buyer {user_id}: {exc}")

    async def _notify_buyer_cryptobot_paid(self, tg_user_id: int, text: str) -> None:
        """
        Шлём покупателю «✅ Оплата получена» через shop-бота. Если бот
        отключён или Telegram недоступен — молча игнорируем (баланс юзер
        увидит сам, когда откроет /balance).
        """
        if self.shop_bot is None:
            return
        try:
            await self.shop_bot.send_message_to_user(tg_user_id, text)
        except Exception as exc:
            logger.warning(
                f"shop notify buyer {tg_user_id} cryptobot paid: {exc}"
            )

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
        balance_text: str
        try:
            # Переиспользуем долгоживущий NSClient процесса — он уже
            # держит валидный токен (≤2ч), не делает повторный login.
            # Раньше тут создавался новый NSClient на каждый heartbeat,
            # из-за чего при недоступности NS в логах было «Сеть упала
            # на login: ...» в каждом heartbeat-сообщении.
            ns = self.ns
            if ns is None:
                balance_text = "<i>NS-клиент не инициализирован</i>"
            else:
                bal = await asyncio.wait_for(ns.check_balance(), timeout=10.0)
                balance_text = f"{bal.balance}"
        except asyncio.TimeoutError:
            balance_text = "<i>timeout (NS не отвечает)</i>"
        except Exception as exc:
            balance_text = f"<i>n/a ({type(exc).__name__}: {str(exc)[:80]})</i>"
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
