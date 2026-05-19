"""
FunPay watcher: слушает события и раздаёт callbacks:
    on_new_order(FunPayOrderEvent)
    on_new_message(FunPayMessageEvent)

Архитектура (после долгой битвы с FunPayAPI 1.1.0):

1. listen-loop — крутит `Runner.listen()` библиотеки. Авто-перезапуск,
   если генератор тихо завершился.
2. poll-loop — независимый poller: раз в N секунд тянет
   `account.get_chats()` сам, диффает с предыдущим снимком, на любое
   входящее сообщение зовёт handler. Это спасает в случае, когда
   listen() ничего не присылает (а такое в этой версии библиотеки
   реально бывает: после `INITIAL_CHAT` поток событий пропадает).
3. dedup-кеш — гарантирует, что одно и то же сообщение не сработает
   дважды (когда оно пришло и через listen, и через poll).

Оба цикла живут в отдельных daemon-threads. События в asyncio
прокидываем через `asyncio.run_coroutine_threadsafe`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as concurrent_futures
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from src.funpay.client import FunPayClient
from src.funpay.events import FunPayMessageEvent
from src.orders.processor import FunPayOrderEvent


OrderCallback = Callable[[FunPayOrderEvent], Awaitable[None]]
MessageCallback = Callable[[FunPayMessageEvent], Awaitable[None]]


# Типы событий, которые в разных версиях FunPayAPI означают
# «в чате появилось новое сообщение».
MESSAGE_TYPES = (
    "NEW_MESSAGE",
    "MESSAGE_NEW",
    "CHAT_MESSAGE",
    "NEW_CHAT_MESSAGE",
    "MESSAGE",
    "LAST_CHAT_MESSAGE_CHANGED",
    "CHATS_LIST_CHANGED",
)
ORDER_TYPES = (
    "NEW_ORDER",
    "ORDER_NEW",
    "ORDERS_LIST_CHANGED",
)
# Шум, который не интересен ни handler-у, ни нам в логах.
NOISE_TYPES = (
    "INITIAL_CHAT",
    "INITIAL_ORDER",
)


class FunPayWatcher:
    """Запускает фоновый слушатель + poller и зовёт callbacks на каждое
    релевантное событие."""

    def __init__(
        self,
        fp_client: FunPayClient,
        *,
        on_new_order: OrderCallback | None = None,
        on_new_message: MessageCallback | None = None,
        poll_interval_seconds: float = 5.0,
        listen_restart_delay_seconds: float = 5.0,
        dedup_cache_size: int = 1024,
        listen_enabled: bool = True,
    ) -> None:
        self._fp = fp_client
        self._on_new_order = on_new_order
        self._on_new_message = on_new_message
        self._stop_evt = threading.Event()
        self._listen_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poll_interval = poll_interval_seconds
        self._listen_restart_delay = listen_restart_delay_seconds
        self._listen_enabled = listen_enabled

        # Дедуп по message_id (если есть) или по (chat_id, author_id, text)
        # как fallback. Хранит ограниченное число ключей.
        self._seen_keys: deque[tuple[Any, ...]] = deque(maxlen=dedup_cache_size)
        self._seen_lock = threading.Lock()
        # Preview snapshot для polling (для диффа preview между запусками).
        # Source of truth для message_id курсора — таблица funpay_chat_cursors.
        self._poll_snapshot: dict[int, dict[str, Any]] = {}
        # Первый прогон poll_once завершён → нормальный режим.
        self._baseline_ready = threading.Event()

    def _is_my_message(
        self, author_id: Any, author_username: str | None
    ) -> bool:
        """
        Сообщение считается «нашим» если:
        - author_id совпадает с self.account.id, ИЛИ
        - author_username совпадает с self._fp.my_username (case-insensitive).

        Username-фильтр критически важен: FunPay не всегда отдаёт data-author
        в HTML, а без него фильтр только по id пропустит наше сообщение,
        и handler начнёт реагировать на свои же шаблоны.
        """
        my_id = (
            getattr(self._fp, "my_user_id", None)
            or getattr(self._fp.account, "id", None)
        )
        my_username = getattr(self._fp, "my_username", None) or getattr(
            self._fp.account, "username", None
        )
        try:
            if (
                my_id is not None
                and author_id is not None
                and int(author_id) == int(my_id)
            ):
                return True
        except (TypeError, ValueError):
            pass
        if (
            my_username
            and author_username
            and str(author_username).strip().lower()
            == str(my_username).strip().lower()
        ):
            return True
        return False

    def _make_msg_dedup_keys(
        self,
        chat_id: int,
        message_id: Any,
        author_id: Any,
        text: str,
    ) -> list[tuple[Any, ...]]:
        """
        Возвращает СПИСОК ключей дедупа для сообщения.

        Идея: одно и то же сообщение нужно регистрировать сразу по двум
        измерениям — по `message_id` (если он есть) и по тексту. Тогда
        если разные источники (listen-loop / poll-loop) видят это
        сообщение по-разному (один с id, другой без), пересечение хотя
        бы по одному ключу не даст обработать сообщение дважды.
        """
        keys: list[tuple[Any, ...]] = []
        if message_id is not None:
            keys.append(("msg", chat_id, "id", message_id))
        # Хэш текста — стабилен между источниками, даже если
        # message_id где-то теряется.
        keys.append(("msg", chat_id, "text", author_id, hash(text[:200])))
        return keys

    def _seen_or_register(self, keys: list[tuple[Any, ...]]) -> bool:
        """
        True если хоть один из ключей уже встречался (=> дубль, пропустить).
        False если ни одного не было — тогда регистрируем все ключи как
        видимые.

        Регистрация всех ключей сразу гарантирует, что если потом этот
        же msg придёт от другого источника с другим набором ключей, мы
        его всё равно опознаем как уже виденный.
        """
        with self._seen_lock:
            if any(k in self._seen_keys for k in keys):
                return True
            for k in keys:
                self._seen_keys.append(k)
        return False

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._stop_evt.clear()
        self._baseline_ready.clear()
        self._loop = asyncio.get_running_loop()

        if self._listen_enabled:
            self._listen_thread = threading.Thread(
                target=self._listen_loop, name="funpay-watcher-listen", daemon=True
            )
            self._listen_thread.start()

        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="funpay-watcher-poll", daemon=True
        )
        self._poll_thread.start()
        if self._listen_enabled:
            logger.info(
                f"FunPay watcher запущен (listen-thread + poll каждые "
                f"{self._poll_interval:.0f}s)"
            )
        else:
            logger.info(
                f"FunPay watcher запущен (только poll каждые "
                f"{self._poll_interval:.0f}s — listen отключён)"
            )

    def stop(self) -> None:
        self._stop_evt.set()
        for t in (self._listen_thread, self._poll_thread):
            if t is not None:
                t.join(timeout=10)
        logger.info("FunPay watcher остановлен")

    # ---------- listen-loop ----------

    def _listen_loop(self) -> None:
        """
        Крутит Runner.listen() в бесконечном цикле. Если генератор
        тихо завершился (FunPayAPI 1.1.0 это делает после INITIAL_CHAT
        снимка в ряде случаев) — рестартую через короткую паузу.
        """
        while not self._stop_evt.is_set():
            try:
                events_iter = self._get_event_iterator()
            except Exception as exc:
                logger.warning(
                    f"FunPay watcher: не смог получить event-iterator "
                    f"({type(exc).__name__}: {exc}). Повтор через "
                    f"{self._listen_restart_delay}s."
                )
                if self._stop_evt.wait(self._listen_restart_delay):
                    return
                continue

            events_consumed = 0
            try:
                for event in events_iter:
                    if self._stop_evt.is_set():
                        return
                    events_consumed += 1
                    try:
                        self._handle_event(event)
                    except Exception as exc:
                        logger.exception(
                            f"Не получилось обработать FunPay-событие: {exc}"
                        )
            except Exception as exc:
                logger.warning(
                    f"FunPay listen() итерация упала "
                    f"({type(exc).__name__}: {exc}). Перезапускаю через "
                    f"{self._listen_restart_delay}s."
                )

            if self._stop_evt.is_set():
                return
            logger.debug(
                f"FunPay listen() завершил итерацию ({events_consumed} событий). "
                f"Перезапуск через {self._listen_restart_delay}s."
            )
            if self._stop_evt.wait(self._listen_restart_delay):
                return

    def _get_event_iterator(self) -> Any:
        """Возвращает iterable событий FunPay (зависит от версии библиотеки)."""
        account = self._fp.account

        runner = getattr(account, "runner", None)
        if runner is not None:
            listen = getattr(runner, "listen", None)
            if callable(listen):
                return listen()

        get_updates = getattr(account, "get_updates", None)
        if callable(get_updates):
            try:
                return get_updates()
            except TypeError as exc:
                logger.debug(f"account.get_updates() не итерируется: {exc}")

        try:
            from FunPayAPI import Runner  # type: ignore

            runner = Runner(account)
            return runner.listen()
        except Exception as exc:
            logger.debug(f"Runner(account).listen() недоступен: {exc}")

        raise RuntimeError(
            "FunPayAPI: не нашёл способ слушать события. "
            "Запусти `python -m src.tools.funpay_introspect` и пришли вывод."
        )

    # ---------- poll-loop ----------

    def _poll_loop(self) -> None:
        """
        Опрашивает /chat/ FunPay через свой HTTP-клиент с интервалом
        self._poll_interval.

        КРИТИЧНО: вся работа с asyncio (включая HTTP-запросы и БД)
        выполняется в MAIN event loop через `run_coroutine_threadsafe`.
        Если делать `asyncio.run()` прямо в этом thread'е, aiosqlite
        ломается, потому что её engine привязан к loop'у создания.
        """
        time.sleep(2.0)
        while not self._stop_evt.is_set():
            if self._loop is None or not self._loop.is_running():
                logger.warning(
                    "FunPay poll-loop: main event-loop не доступен, "
                    "жду 5s и пробую снова"
                )
                if self._stop_evt.wait(5.0):
                    return
                continue
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._poll_once_async(), self._loop
                )
                future.result(timeout=60)  # ждём результата, но не вечно
            except concurrent_futures.TimeoutError:
                logger.warning(
                    "FunPay poll-loop: итерация заняла > 60 секунд, пропускаю"
                )
            except Exception as exc:
                logger.warning(
                    f"FunPay poll-loop: итерация упала "
                    f"({type(exc).__name__}: {exc})"
                )
            if self._stop_evt.wait(self._poll_interval):
                return

    async def _poll_once_async(self) -> None:
        """
        Одна итерация poll'а. ВСЯ работа — в main asyncio loop.

        Архитектура:
        - Курсор для каждого чата живёт в БД (`funpay_chat_cursors`).
        - Первый прогон (`is_first_run`): историю не разыгрываем, только
          инициализируем курсоры. Последующие новые сообщения ловим.
        - Runtime для нового чата (нет курсора): обрабатываем последнее
          сообщение (то, что вызвало preview-change).
        - Runtime для известного чата (есть курсор): обрабатываем всё
          с id > курсор.
        """
        admin = getattr(self._fp, "_admin", None)
        if admin is None:
            logger.warning("FunPay poll: admin-клиент не инициализирован")
            return

        try:
            items = await admin.get_chats_snapshot()
        except Exception as exc:
            logger.warning(
                f"FunPay poll: get_chats_snapshot упал "
                f"({type(exc).__name__}: {exc})"
            )
            return

        is_first_run = not self._baseline_ready.is_set()
        if is_first_run:
            logger.info(
                f"FunPay poll: первый прогон, {len(items)} чатов в snapshot — "
                f"инициализирую курсоры в БД"
            )

        to_fetch: list[tuple[int, str | None, str]] = []
        for item in items:
            chat_id = item["chat_id"]
            preview = item.get("preview", "")
            username = item.get("username")
            unread = item.get("unread", False)
            prev_state = self._poll_snapshot.get(chat_id) or {}
            if is_first_run:
                to_fetch.append((chat_id, username, preview))
            elif prev_state.get("preview") != preview or unread:
                logger.debug(
                    f"FunPay poll: chat={chat_id} preview изменился "
                    f"({prev_state.get('preview')!r} -> {preview!r}, "
                    f"unread={unread}) → fetch"
                )
                to_fetch.append((chat_id, username, preview))

        total_dispatched = 0
        for chat_id, username, preview in to_fetch:
            cursor_last_id = await self._load_cursor(chat_id)
            try:
                messages = await admin.get_chat_messages(
                    chat_id, last_id=cursor_last_id
                )
            except Exception as exc:
                logger.warning(
                    f"FunPay poll: get_chat_messages({chat_id}) упал "
                    f"({type(exc).__name__}: {exc})"
                )
                continue

            new_messages, new_last_id = self._select_new_messages(
                messages=messages,
                cursor_last_id=cursor_last_id,
                is_first_run=is_first_run,
            )

            self._poll_snapshot[chat_id] = {"preview": preview}
            if new_last_id is not None and new_last_id != cursor_last_id:
                await self._save_cursor(chat_id, new_last_id)

            for m in new_messages:
                author_id = m.get("author_id")
                author_username = m.get("author_username") or username
                if self._is_my_message(author_id, author_username):
                    continue
                msg = FunPayMessageEvent(
                    chat_id=chat_id,
                    chat_username=username,
                    author_id=author_id,
                    author_username=author_username,
                    text=m.get("text", ""),
                    is_my_message=False,
                )
                if not msg.text:
                    continue
                keys = self._make_msg_dedup_keys(
                    chat_id, m.get("message_id"), author_id, msg.text
                )
                if self._seen_or_register(keys):
                    logger.debug(
                        f"FunPay poll: chat={chat_id} msg "
                        f"id={m.get('message_id')} text={msg.text[:60]!r} — "
                        f"дубликат, пропускаю"
                    )
                    continue
                logger.info(
                    f"FunPay poll: новое сообщение в чате {chat_id} от "
                    f"@{msg.author_username} (id={m.get('message_id')}): "
                    f"{msg.text[:80]!r}"
                )
                total_dispatched += 1
                if self._on_new_message is not None:
                    # ВНУТРИ main loop → просто await/create_task
                    asyncio.create_task(
                        self._safe_call_message_handler(msg)
                    )

        if is_first_run:
            self._baseline_ready.set()
            logger.info(
                f"FunPay poll: первый прогон завершён — {len(to_fetch)} "
                f"чатов синхронизированы с БД-курсорами"
            )
        elif total_dispatched > 0:
            logger.info(
                f"FunPay poll: dispatched {total_dispatched} новое(ых) сообщение(й)"
            )

    async def _safe_call_message_handler(self, msg: FunPayMessageEvent) -> None:
        """Обёртка вокруг message-handler с логом исключений."""
        try:
            if self._on_new_message is not None:
                await self._on_new_message(msg)
        except Exception as exc:
            logger.opt(exception=exc).error(
                f"ChatHandler упал на сообщении chat={msg.chat_id} "
                f"text={msg.text[:80]!r}: {exc}"
            )

    def _select_new_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        cursor_last_id: int | None,
        is_first_run: bool,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """
        Выбирает «новые» сообщения для dispatch и новый last_id для курсора.

        Правила:
        - cursor_last_id известен → диспатчим всё с id > cursor_last_id.
        - cursor_last_id is None, is_first_run=True → baseline без dispatch,
          курсор = max(id).
        - cursor_last_id is None, is_first_run=False → новый чат runtime,
          диспатчим последнее сообщение из выборки.
        """
        if not messages:
            return [], cursor_last_id

        max_id = max(
            (m.get("message_id") for m in messages if m.get("message_id") is not None),
            default=None,
        )

        if cursor_last_id is not None:
            new = [
                m for m in messages
                if m.get("message_id") is not None
                and m["message_id"] > cursor_last_id
            ]
            return new, max_id or cursor_last_id

        if is_first_run:
            return [], max_id

        return [messages[-1]], max_id

    async def _load_cursor(self, chat_id: int) -> int | None:
        """Курсор из БД (last_message_id или None)."""
        from src.db.repo import get_chat_cursor
        from src.db.session import session_factory
        try:
            async with session_factory()() as session:
                cursor = await get_chat_cursor(session, chat_id)
                return cursor.last_message_id if cursor else None
        except Exception as exc:
            logger.warning(
                f"_load_cursor({chat_id}) упал: {type(exc).__name__}: {exc}. "
                f"Чат будет работать без БД-курсора (in-memory only)."
            )
            return None

    async def _save_cursor(self, chat_id: int, last_message_id: int) -> None:
        """Сохраняет курсор в БД (двигаем только вперёд)."""
        from src.db.repo import upsert_chat_cursor
        from src.db.session import session_factory
        try:
            async with session_factory()() as session:
                await upsert_chat_cursor(
                    session,
                    chat_id=chat_id,
                    last_message_id=last_message_id,
                )
                await session.commit()
        except Exception as exc:
            logger.warning(
                f"_save_cursor({chat_id}, {last_message_id}) упал: "
                f"{type(exc).__name__}: {exc}"
            )

    # ---------- dedup ----------

    def _dedup_register(self, kind: str, payload: Any) -> bool:
        """Регистрирует ключ; True если ключ новый (= обрабатывать)."""
        if kind == "msg" and isinstance(payload, FunPayMessageEvent):
            # listen-loop обычно НЕ знает message_id (FunPayAPI его не
            # отдаёт в этой версии), но poll-loop знает. Регистрируем по
            # обоим ключам — text-hash совпадёт между источниками.
            msg_id = getattr(payload, "message_id", None)
            keys = self._make_msg_dedup_keys(
                payload.chat_id, msg_id, payload.author_id, payload.text
            )
            return not self._seen_or_register(keys)
        if kind == "order" and isinstance(payload, FunPayOrderEvent):
            order_key = ("order", payload.funpay_order_id)
            with self._seen_lock:
                if order_key in self._seen_keys:
                    return False
                self._seen_keys.append(order_key)
            return True
        return True

    # ---------- event handling ----------

    def _handle_event(self, event: Any) -> None:
        event_type = self._extract_event_type(event)
        type_str = str(event_type).upper() if event_type is not None else ""

        # INITIAL_* — это снимок на старте listen(). Шумит. На DEBUG.
        if any(noise in type_str for noise in NOISE_TYPES):
            logger.debug(f"FunPay event (noise): type={type_str!r}")
            return

        logger.info(f"FunPay event: type={type_str!r}")

        if any(t in type_str for t in ORDER_TYPES):
            order = self._normalize_order(event)
            if order is None:
                return
            if not self._dedup_register("order", order):
                return
            if self._on_new_order is None:
                return
            logger.info(
                f"FunPay NEW_ORDER: order={order.funpay_order_id}, "
                f"lot={order.funpay_lot_id}, qty={order.quantity}"
            )
            self._dispatch_async(self._on_new_order(order))
            return

        if any(t in type_str for t in MESSAGE_TYPES):
            msg = self._normalize_message(event)
            if msg is None:
                logger.debug(
                    f"FunPay {type_str}: не смог нормализовать (нет text?): "
                    f"event={event!r}"
                )
                return
            if msg.is_my_message:
                return
            if not self._dedup_register("msg", msg):
                # Уже видели через poller — игнорируем.
                return
            if self._on_new_message is None:
                return
            logger.info(
                f"FunPay NEW_MESSAGE: chat={msg.chat_id}, "
                f"author=@{msg.author_username}, "
                f"text={msg.text[:80]!r}"
            )
            self._dispatch_async(self._on_new_message(msg))
            return

    def _dispatch_async(self, coro) -> None:
        if self._loop is None:
            logger.warning("Нет asyncio-loop для dispatch события")
            return
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._on_dispatch_done)

    @staticmethod
    def _on_dispatch_done(future: Any) -> None:
        try:
            exc = future.exception()
        except Exception as e:
            logger.warning(f"Не смог прочитать future.exception(): {e}")
            return
        if exc is not None:
            logger.opt(exception=exc).error(
                f"Неперехваченное исключение в dispatched FunPay handler: {exc}"
            )

    # ---------- нормализация event объектов ----------

    def _normalize_order(self, event: Any) -> Optional[FunPayOrderEvent]:
        order_obj = (
            getattr(event, "order_obj", None)
            or getattr(event, "order", None)
            or getattr(event, "data", None)
            or event
        )

        funpay_order_id = self._g(order_obj, "id", "order_id", "ID")
        if funpay_order_id is None:
            logger.warning(f"Не нашёл order_id в событии: {event!r}")
            return None
        funpay_order_id = str(funpay_order_id)

        funpay_lot_id_raw = self._g(order_obj, "lot_id", "offer_id")
        try:
            funpay_lot_id = int(funpay_lot_id_raw) if funpay_lot_id_raw is not None else 0
        except (TypeError, ValueError):
            funpay_lot_id = 0
        if funpay_lot_id <= 0:
            logger.warning(
                f"FunPay NEW_ORDER: не нашёл валидный lot_id, "
                f"order={funpay_order_id}, raw={order_obj!r}. Пропускаю."
            )
            return None

        buyer = self._g(order_obj, "buyer", "buyer_username", "username")
        buyer_username = (
            getattr(buyer, "username", None)
            if buyer is not None and not isinstance(buyer, str)
            else buyer
        )
        if buyer_username is not None and not isinstance(buyer_username, str):
            buyer_username = str(buyer_username)

        buyer_user_id_raw = self._g(order_obj, "buyer_id", "user_id")
        try:
            buyer_user_id = int(buyer_user_id_raw) if buyer_user_id_raw is not None else None
        except (TypeError, ValueError):
            buyer_user_id = None

        chat_id_raw = self._g(order_obj, "chat_id", "node_id")
        try:
            chat_id = int(chat_id_raw) if chat_id_raw is not None else None
        except (TypeError, ValueError):
            chat_id = None

        quantity_raw = self._g(order_obj, "amount", "quantity", "count")
        try:
            quantity = int(quantity_raw) if quantity_raw is not None else 1
        except (TypeError, ValueError):
            quantity = 1

        price_raw = self._g(order_obj, "price", "sum", "total")
        try:
            funpay_price_rub = float(price_raw) if price_raw is not None else None
        except (TypeError, ValueError):
            funpay_price_rub = None

        return FunPayOrderEvent(
            funpay_order_id=funpay_order_id,
            funpay_lot_id=funpay_lot_id,
            buyer_username=buyer_username,
            buyer_user_id=buyer_user_id,
            chat_id=chat_id,
            quantity=max(1, quantity),
            funpay_price_rub=funpay_price_rub,
        )

    def _normalize_message(self, event: Any) -> Optional[FunPayMessageEvent]:
        msg_obj = (
            getattr(event, "message_obj", None)
            or getattr(event, "message", None)
            or getattr(event, "data", None)
            or event
        )
        # LAST_CHAT_MESSAGE_CHANGED шлёт chat-объект как .data; внутри последнее
        # сообщение лежит в .last_message
        if hasattr(msg_obj, "last_message"):
            inner = getattr(msg_obj, "last_message")
            if inner is not None:
                # для нормализации нам нужен и текст, и автор — last_message несёт оба
                msg_obj = inner

        text_raw = self._g(msg_obj, "text", "content", "body")
        if text_raw is None:
            return None
        text = str(text_raw)

        chat_id_raw = self._g(msg_obj, "chat_id", "node_id", "node")
        try:
            chat_id = int(chat_id_raw) if chat_id_raw is not None else None
        except (TypeError, ValueError):
            chat_id = None
        if chat_id is None:
            # fallback: chat_id может быть на исходном event-е
            outer_chat = self._g(event, "chat_id", "node_id")
            try:
                chat_id = int(outer_chat) if outer_chat is not None else None
            except (TypeError, ValueError):
                chat_id = None
        if chat_id is None:
            return None

        chat_username = self._g(msg_obj, "chat_name", "chat_username", "interlocutor")

        author = self._g(msg_obj, "author", "from_user", "user")
        author_id = self._g(msg_obj, "author_id", "user_id")
        author_username = None
        if author is not None and not isinstance(author, (int, str)):
            author_username = getattr(author, "username", None)
            author_id = author_id or getattr(author, "id", None)
        elif isinstance(author, str):
            author_username = author
        try:
            author_id = int(author_id) if author_id is not None else None
        except (TypeError, ValueError):
            author_id = None

        normalized_username = (
            str(author_username) if author_username is not None else None
        )
        is_my = self._is_my_message(author_id, normalized_username)

        return FunPayMessageEvent(
            chat_id=chat_id,
            chat_username=str(chat_username) if chat_username is not None else None,
            author_id=author_id,
            author_username=normalized_username,
            text=text,
            is_my_message=is_my,
        )

    @staticmethod
    def _extract_event_type(event: Any) -> Any:
        for attr in ("type", "event_type", "kind"):
            value = getattr(event, attr, None)
            if value is not None:
                return value
        if isinstance(event, dict):
            return event.get("type") or event.get("event_type")
        return None

    @staticmethod
    def _g(obj: Any, *attrs: str) -> Any:
        for attr in attrs:
            value = getattr(obj, attr, None)
            if value is not None:
                return value
            if isinstance(obj, dict) and attr in obj:
                return obj[attr]
        return None
