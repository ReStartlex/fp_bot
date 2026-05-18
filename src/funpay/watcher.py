"""
FunPay watcher: слушает события (Runner.listen()) в фоне и раздаёт callbacks:
    on_new_order(FunPayOrderEvent)
    on_new_message(FunPayMessageEvent)

FunPayAPI работает блокирующе (requests + sync generator). listen() крутится
в отдельном thread, события через run_coroutine_threadsafe попадают в asyncio.

API библиотеки FunPayAPI разное в разных форках, поэтому атрибуты ищем
через getattr-fallback.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from src.funpay.client import FunPayClient
from src.funpay.events import FunPayMessageEvent
from src.orders.processor import FunPayOrderEvent


OrderCallback = Callable[[FunPayOrderEvent], Awaitable[None]]
MessageCallback = Callable[[FunPayMessageEvent], Awaitable[None]]


class FunPayWatcher:
    """Запускает фоновый слушатель и зовёт callbacks на каждое релевантное событие."""

    def __init__(
        self,
        fp_client: FunPayClient,
        *,
        on_new_order: OrderCallback | None = None,
        on_new_message: MessageCallback | None = None,
    ) -> None:
        self._fp = fp_client
        self._on_new_order = on_new_order
        self._on_new_message = on_new_message
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._run, name="funpay-watcher", daemon=True
        )
        self._thread.start()
        logger.info("FunPay watcher запущен")

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        logger.info("FunPay watcher остановлен")

    # ---------- Внутреннее ----------

    def _run(self) -> None:
        try:
            self._listen_blocking()
        except Exception as exc:
            logger.exception(f"FunPay watcher упал: {exc}")

    def _get_event_iterator(self) -> Any:
        """Возвращает iterable событий FunPay (зависит от версии библиотеки)."""
        account = self._fp.account

        runner = getattr(account, "runner", None)
        if runner is not None:
            listen = getattr(runner, "listen", None)
            if callable(listen):
                logger.debug("FunPay watcher: использую account.runner.listen()")
                return listen()

        get_updates = getattr(account, "get_updates", None)
        if callable(get_updates):
            try:
                gen = get_updates()
                logger.debug("FunPay watcher: использую account.get_updates()")
                return gen
            except TypeError as exc:
                logger.debug(f"account.get_updates() не итерируется: {exc}")

        try:
            from FunPayAPI import Runner  # type: ignore

            runner = Runner(account)
            logger.debug("FunPay watcher: использую Runner(account).listen()")
            return runner.listen()
        except Exception as exc:
            logger.debug(f"Runner(account).listen() недоступен: {exc}")

        raise RuntimeError(
            "FunPayAPI: не нашёл способ слушать события. "
            "Сделай FunPayClient.describe_account() и пришли вывод."
        )

    def _listen_blocking(self) -> None:
        events_iter = self._get_event_iterator()
        for event in events_iter:
            if self._stop_evt.is_set():
                break
            try:
                self._handle_event(event)
            except Exception as exc:
                logger.exception(f"Не получилось обработать FunPay-событие: {exc}")

    def _handle_event(self, event: Any) -> None:
        event_type = self._extract_event_type(event)
        type_str = str(event_type).upper() if event_type is not None else ""

        if "NEW_ORDER" in type_str or "ORDER_NEW" in type_str:
            order = self._normalize_order(event)
            if order is not None and self._on_new_order is not None:
                logger.info(
                    f"FunPay NEW_ORDER: order={order.funpay_order_id}, "
                    f"lot={order.funpay_lot_id}, qty={order.quantity}"
                )
                self._dispatch_async(self._on_new_order(order))
            return

        if "NEW_MESSAGE" in type_str or "MESSAGE_NEW" in type_str or "MESSAGE" == type_str:
            msg = self._normalize_message(event)
            if msg is not None and self._on_new_message is not None:
                logger.debug(
                    f"FunPay NEW_MESSAGE: chat={msg.chat_id}, "
                    f"author={msg.author_username}, my={msg.is_my_message}, "
                    f"text={msg.text[:60]!r}"
                )
                self._dispatch_async(self._on_new_message(msg))
            return

    def _dispatch_async(self, coro) -> None:
        if self._loop is None:
            logger.warning("Нет asyncio-loop для dispatch события")
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ---------- Нормализация ----------

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

        funpay_lot_id_raw = self._g(order_obj, "lot_id", "subcategory_id", "node_id", "offer_id")
        try:
            funpay_lot_id = int(funpay_lot_id_raw) if funpay_lot_id_raw is not None else 0
        except (TypeError, ValueError):
            funpay_lot_id = 0

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
        # В разных версиях message-объект лежит в event.message_obj / event.message / event.data
        msg_obj = (
            getattr(event, "message_obj", None)
            or getattr(event, "message", None)
            or getattr(event, "data", None)
            or event
        )
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

        # "Моё" сообщение определяем по account.id
        my_id = getattr(self._fp.account, "id", None)
        is_my = bool(my_id is not None and author_id is not None and int(author_id) == int(my_id))

        return FunPayMessageEvent(
            chat_id=chat_id,
            chat_username=str(chat_username) if chat_username is not None else None,
            author_id=author_id,
            author_username=str(author_username) if author_username is not None else None,
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
