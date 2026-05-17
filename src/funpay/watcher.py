"""
FunPay watcher: слушает события (Runner.listen()) в фоне и отдаёт нам
нормализованные FunPayOrderEvent.

ВАЖНО: FunPayAPI работает блокирующе (requests + sync generator). Поэтому
listen()-цикл крутится в отдельном thread, а callback вызывается в основном
asyncio-loop через `run_coroutine_threadsafe`.

API у FunPayAPI разное в разных версиях (sidor0912 vs форки), поэтому мы
аккуратно ищем нужные атрибуты через getattr, иначе явно логируем что нашли.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from src.funpay.client import FunPayClient
from src.orders.processor import FunPayOrderEvent


OrderCallback = Callable[[FunPayOrderEvent], Awaitable[None]]


class FunPayWatcher:
    """
    Запускает слушатель FunPay-событий и вызывает on_new_order для каждой
    новой покупки.

    Реализация специально терпима к разным версиям библиотеки FunPayAPI:
    - пытается использовать `account.runner` если он есть
    - иначе `account.get_updates()` как генератор
    - иначе honest error в лог
    """

    def __init__(
        self,
        fp_client: FunPayClient,
        on_new_order: OrderCallback,
    ) -> None:
        self._fp = fp_client
        self._on_new_order = on_new_order
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

        # Вариант 1: account.runner.listen()
        runner = getattr(account, "runner", None)
        if runner is not None:
            listen = getattr(runner, "listen", None)
            if callable(listen):
                logger.debug("FunPay watcher: использую account.runner.listen()")
                return listen()

        # Вариант 2: account.get_updates() — это generator
        get_updates = getattr(account, "get_updates", None)
        if callable(get_updates):
            try:
                gen = get_updates()
                logger.debug("FunPay watcher: использую account.get_updates() как iterable")
                return gen
            except TypeError as exc:
                logger.debug(f"account.get_updates() не итерируется: {exc}")

        # Вариант 3: создать Runner вручную
        try:
            from FunPayAPI import Runner  # type: ignore

            runner = Runner(account)
            logger.debug("FunPay watcher: использую Runner(account).listen()")
            return runner.listen()
        except Exception as exc:
            logger.debug(f"Runner(account).listen() недоступен: {exc}")

        raise RuntimeError(
            "FunPayAPI: не нашёл способ слушать события. "
            "Сделай `FunPayClient.describe_account()` и пришли вывод."
        )

    def _listen_blocking(self) -> None:
        events_iter = self._get_event_iterator()
        for event in events_iter:
            if self._stop_evt.is_set():
                break
            try:
                normalized = self._normalize_event(event)
            except Exception as exc:
                logger.exception(f"Не получилось распарсить FunPay-событие: {exc}")
                continue
            if normalized is None:
                continue

            logger.info(
                f"FunPay event NEW_ORDER: order={normalized.funpay_order_id}, "
                f"lot={normalized.funpay_lot_id}, qty={normalized.quantity}"
            )
            self._dispatch(normalized)

    def _dispatch(self, event: FunPayOrderEvent) -> None:
        """Перекинуть событие в asyncio-loop основной программы."""
        if self._loop is None:
            logger.warning("Нет asyncio-loop для dispatch, пропускаю событие")
            return
        future = asyncio.run_coroutine_threadsafe(self._on_new_order(event), self._loop)
        # Не блокируемся ожиданием — пайплайн заказа может длиться минутами

    def _normalize_event(self, event: Any) -> Optional[FunPayOrderEvent]:
        """
        Привести событие FunPayAPI к нашему FunPayOrderEvent.
        Поддерживается несколько форматов:
        - event.type == EventTypes.NEW_ORDER + event.order_obj/order/data
        - event.NEW_ORDER флаг
        - dict
        """
        # 1. Тип события
        event_type = self._extract_event_type(event)
        if event_type is None:
            return None

        type_str = str(event_type).upper()
        if "NEW_ORDER" not in type_str and "ORDER_NEW" not in type_str:
            # нас интересуют только новые заказы (chat-сообщения обработаем отдельно позже)
            return None

        # 2. Достаём данные заказа
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

        buyer_username = self._g(order_obj, "buyer", "buyer_username", "seller_username", "username")
        if buyer_username is not None and hasattr(buyer_username, "username"):
            buyer_username = buyer_username.username  # type: ignore[union-attr]
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
