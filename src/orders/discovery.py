"""Order discovery: 3-й канал получения заказов с FunPay.

Зачем существует
================

В системе три канала, по которым новый заказ может попасть в processor:

1. ``FunPayWatcher`` listen-loop — события NEW_ORDER из ``Runner.listen()``
   библиотеки FunPayAPI. По умолчанию ВЫКЛЮЧЕН (``FUNPAY_LISTEN_ENABLED=false``),
   потому что в FunPayAPI 1.1.0 он шумит и часто «тихо умирает» после
   INITIAL_CHAT.
2. ``FunPayWatcher`` poll-loop — слушает сообщения в чатах, но ORDER-события
   в нём не обрабатываются. Системные сообщения «оплачен заказ #...»
   ловит ``ChatHandler``, и он лишь помечает их в ``ChatState``,
   а собственно ``Order`` в БД не создаёт.
3. **Этот модуль** — раз в N секунд берёт свежий список paid-заказов через
   ``account.get_sells(state="paid")`` и для каждого, которого ещё нет в БД,
   вызывает ``process_funpay_order``. Это даёт ленивый (≤60с задержка),
   но надёжный канал — заказ всегда оказывается на странице
   `funpay.com/orders/trade` сразу после оплаты.

Безопасность
============

* **Идемпотентность** — гарантирует ``process_funpay_order`` через per-key
  asyncio.Lock и state machine в БД. Если discovery подаст тот же
  ``funpay_order_id`` снова (например, во время первого выполнения),
  второй вызов увидит ``status == ns_created`` и продолжит с этого шага
  без дубля NS-покупки.
* **Дедуп через БД** — `find_order_by_funpay_id`. Это эффективно при
  максимум ~50 заказах за страницу, что укладывается в один SELECT по
  индексу uq_orders_funpay.
* **manual_hold / failed заказы не трогаем** — пропускаем по результату
  pre-check.

Никаких side-effects, если FunPay недоступен: ловим Exception на самом
HTTP, возвращаем «0 новых» и логируем WARNING — следующий тик попробует
снова.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from src.alerts.telegram import TelegramNotifier
from src.config import Settings, get_settings
from src.db.repo import find_order_by_funpay_id
from src.db.session import session_factory
from src.funpay.client import FunPayClient, RecentSale
from src.ns import NSClient
from src.orders.processor import FunPayOrderEvent, process_funpay_order


# Статусы, которые означают «бот уже знает об этом заказе и завершил с ним
# дело» — discovery должен молча пропускать такие, чтобы не дёргать
# processor зря. Активные статусы (received/ns_created/…) тоже пропускаем:
# их подберёт reconciler через свой APScheduler-job.
_TERMINAL_STATUSES = frozenset(
    {"delivered", "failed", "manual_hold", "external_delivered"}
)
_ACTIVE_STATUSES = frozenset(
    {"received", "ns_created", "ns_paid", "pins_ready", "delivering"}
)


@dataclass(frozen=True)
class DiscoveryResult:
    """Метрики одного прогона discovery."""

    fetched: int = 0          # сколько paid-заказов вернул FunPay
    already_known: int = 0    # уже есть в БД — пропустили
    dispatched: int = 0       # запустили processor на новый order
    delivered: int = 0        # processor успешно довёл до delivered
    failed: int = 0           # processor вернул failed
    skipped: int = 0          # manual_hold / pins_ready / ns_created (отложили)
    errors: int = 0           # исключения внутри processor (НЕ внутри FunPay HTTP)

    def as_dict(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "already_known": self.already_known,
            "dispatched": self.dispatched,
            "delivered": self.delivered,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def _build_event(sale: RecentSale) -> FunPayOrderEvent:
    """Строит FunPayOrderEvent из RecentSale.

    chat_id не знаем из orders-page (FunPay его не отдаёт в OrderShortcut);
    processor сам попробует разрешить через ``account.get_chat_by_name``
    по buyer_username в ``_resolve_chat_id``.
    """
    return FunPayOrderEvent(
        funpay_order_id=sale.order_id,
        funpay_lot_id=sale.funpay_lot_id,
        buyer_username=sale.buyer_username,
        buyer_user_id=sale.buyer_user_id,
        chat_id=None,
        quantity=sale.quantity,
        funpay_price_rub=sale.price_rub,
        description=sale.description,
    )


async def discover_new_orders_once(
    *,
    funpay_client: FunPayClient | None,
    ns_client: NSClient | None = None,
    settings: Settings | None = None,
    telegram: TelegramNotifier | None = None,
    max_per_run: int | None = None,
) -> DiscoveryResult:
    """Один прогон discovery: ищем paid-заказы, которых нет в БД, и обрабатываем.

    Параметры
    ---------
    funpay_client:
        Подключённый ``FunPayClient``. Если ``None`` — discovery невозможен
        и мы возвращаем пустой результат (это норма при ``_try_connect_funpay``
        не успевшем подключиться).
    ns_client:
        Активный ``NSClient`` — передаётся в ``process_funpay_order``, чтобы
        он не создавал свой собственный, тратя лишний токен.
    settings:
        Глобальные настройки (читаем ``funpay_order_discovery_max_per_run``,
        если ``max_per_run`` не передан явно).
    telegram:
        Notifier для алертов processor'а. Дискавери сам в TG ничего не
        отсылает — это технический фоновый цикл, а уведомления о результате
        идут уже из ``process_funpay_order`` / ``order_success``.
    max_per_run:
        Опциональный override лимита. По умолчанию берётся из настроек.

    Возвращает
    ----------
    ``DiscoveryResult`` с метриками. Никогда не бросает наружу.
    """
    settings = settings or get_settings()
    if not settings.funpay_order_discovery_enabled:
        return DiscoveryResult()
    if funpay_client is None:
        logger.debug("order discovery: FunPay-клиент не подключён, skip")
        return DiscoveryResult()

    limit = (
        int(max_per_run)
        if max_per_run is not None
        else int(settings.funpay_order_discovery_max_per_run)
    )
    limit = max(1, limit)

    # Один запрос за свежей верхушкой ленты paid-заказов. Если FunPay
    # отдаст 0 — это норма. Падение HTTP уже обработано внутри
    # get_recent_sales: вернёт [] и залогирует WARNING.
    sales = await funpay_client.get_recent_sales(state="paid", max_pages=1)
    result = DiscoveryResult(fetched=len(sales))
    if not sales:
        return result

    # На отдельной странице FunPay сначала идут самые свежие. Обрабатываем
    # их в обратном порядке (сначала старые), чтобы при ограниченном
    # max_per_run сначала закрыть «застрявшие» заказы, а свежие — на
    # следующем тике, когда NS-бюджет восстановится.
    dispatched = 0
    for sale in reversed(sales):
        if dispatched >= limit:
            break

        # Discovery работает только с paid: closed/refunded — это уже
        # завершившиеся снаружи заказы. Подбирать их постфактум опасно
        # (NS-покупка дублем), и бессмысленно (товар уже выдан/возвращён).
        if sale.status != "paid":
            continue

        async with session_factory()() as session:
            existing = await find_order_by_funpay_id(session, sale.order_id)
        if existing is not None:
            status = (existing.status or "").lower()
            if status in _TERMINAL_STATUSES or status in _ACTIVE_STATUSES:
                result = _bump(result, already_known=1)
                continue
            # Прочие статусы — на всякий случай пропустим как known: лучше
            # ничего не делать, чем поднять зомби-обработку.
            result = _bump(result, already_known=1)
            continue

        event = _build_event(sale)
        log = logger.bind(funpay_order_id=sale.order_id, source="discovery")
        log.info(
            f"order discovery: новый paid-заказ #{sale.order_id} "
            f"(lot={sale.funpay_lot_id}, qty={sale.quantity}, "
            f"price={sale.price_rub}, buyer=@{sale.buyer_username}) "
            "— запускаю processor"
        )
        dispatched += 1
        try:
            processor_result = await process_funpay_order(
                event,
                settings=settings,
                ns_client=ns_client,
                funpay_client=funpay_client,
                telegram=telegram,
            )
        except Exception as exc:
            log.opt(exception=exc).error(
                f"order discovery: processor упал для #{sale.order_id}: {exc}"
            )
            result = _bump(result, dispatched=1, errors=1)
            continue

        status = (processor_result.get("status") or "").lower()
        if status == "delivered":
            result = _bump(result, dispatched=1, delivered=1)
        elif status == "failed":
            result = _bump(result, dispatched=1, failed=1)
        else:
            # ns_created / ns_paid / pins_ready / manual_hold — заказ
            # «в пути», reconciler доведёт до конца на след. тиках.
            result = _bump(result, dispatched=1, skipped=1)

    return result


def _bump(result: DiscoveryResult, **deltas: int) -> DiscoveryResult:
    """Иммутабельный апдейт DiscoveryResult — складываем counters.

    Дёшево и без подводных камней: dataclass frozen=True, и мы не
    рискуем неаккуратной мутацией shared-state.
    """
    current = result.as_dict()
    for key, delta in deltas.items():
        current[key] = current.get(key, 0) + int(delta)
    return DiscoveryResult(**current)
