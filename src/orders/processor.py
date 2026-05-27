"""
Pipeline обработки FunPay-заказа → NS-покупка → доставка кодов.

Принципы:

1. **Идемпотентность**. Безопасно вызвать несколько раз с тем же
   `funpay_order_id`. Состояние хранится в БД, при повторном входе
   функция продолжает с того шага, на котором остановилась.

2. **Защита от двойной обработки**. На каждый `funpay_order_id` берётся
   per-key asyncio.Lock — вторая параллельная обработка ждёт первую.

3. **Разделение «оплачено в NS» и «доставлено клиенту»**. Если NS
   списал деньги и вернул pins, но FunPay-чат недоступен — pins
   сохраняются в БД, статус становится `pins_ready`. При следующем
   входе (вручную, или повторно из watcher'а) функция повторит
   только доставку, не дёргая NS заново. Деньги уже списаны — пины
   обязаны дойти до клиента.

Статусы Order.status:
    received      — занесли заказ в БД, маппинг найден
    ns_created    — NS create_order успешно (списания нет)
    ns_paid       — NS pay_order успешно, ждём pins
    pins_ready    — pins получены, ещё не доставлены клиенту
    delivered     — пины уже у клиента в чате FunPay
    failed        — нельзя продолжить (нет маппинга / отказ NS / тайм-аут)
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from sqlalchemy import select

from src.alerts.telegram import TelegramNotifier
from src.chat import templates
from src.config import Settings, get_settings
from src.db.models import KnownLot, Mapping, Order
from src.db.repo import (
    create_order,
    find_order_by_funpay_id,
    invalidate_mapping_cache_for_funpay_lot,
    update_order,
)
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.mapping.rules import estimate_profit_rub
from src.ns import NSClient
from src.ns.exceptions import (
    NSError,
    NSInsufficientFunds,
    NSNotFoundError,
    NSOrderTimeoutError,
)
from src.ns.models import OrderInfo, OrderStatus
from src.sync.fx import get_usd_rub_rate


# Per-key мьютекс: гарантирует, что одновременно над одним заказом
# работает только одна корутина (на этот процесс).
_order_locks: dict[str, asyncio.Lock] = {}


def _lock_for(funpay_order_id: str) -> asyncio.Lock:
    lock = _order_locks.get(funpay_order_id)
    if lock is None:
        lock = asyncio.Lock()
        _order_locks[funpay_order_id] = lock
    return lock


@dataclass
class FunPayOrderEvent:
    """Нормализованные данные FunPay-заказа, которые нам нужны."""
    funpay_order_id: str
    funpay_lot_id: int
    buyer_username: Optional[str]
    buyer_user_id: Optional[int]
    chat_id: Optional[int]
    quantity: int = 1
    funpay_price_rub: Optional[float] = None
    description: Optional[str] = None


def _norm_text(value: str | None) -> str:
    raw = (value or "").lower()
    raw = raw.replace("ё", "е")
    return " ".join(re.findall(r"[a-zа-я0-9]+", raw))


_WEAK_MATCH_TOKENS = {
    "gift",
    "card",
    "карта",
    "подарочная",
    "автовыдача",
    "auto",
    "delivery",
}


def _text_tokens(value: str | None) -> set[str]:
    tokens = set(_norm_text(value).split())
    return {
        t for t in tokens
        if (len(t) > 1 or t.isdigit()) and t not in _WEAK_MATCH_TOKENS
    }


def _mapping_match_score(
    *,
    description: str | None,
    mapping: Mapping,
    known_title: str | None,
) -> int:
    desc_norm = _norm_text(description)
    if not desc_norm:
        return 0

    score = 0
    for source, exact_bonus in (
        (mapping.label, 100),
        (known_title, 120),
    ):
        source_norm = _norm_text(source)
        if not source_norm:
            continue
        if source_norm in desc_norm or desc_norm in source_norm:
            score += exact_bonus
        common = _text_tokens(description) & _text_tokens(source)
        score += len(common) * 10
        # Совпавшие числа вроде 2/5/10 USD особенно важны для Apple cards.
        score += sum(15 for token in common if token.isdigit())
    return score


async def _resolve_mapping(event: FunPayOrderEvent, log) -> Mapping | None:
    async with session_factory()() as session:
        if event.funpay_lot_id > 0:
            result = await session.execute(
                select(Mapping).where(Mapping.funpay_lot_id == event.funpay_lot_id)
            )
            return result.scalar_one_or_none()

        result = await session.execute(
            select(Mapping).where(Mapping.enabled.is_(True))
        )
        enabled = list(result.scalars().all())
        known_rows = {}
        if enabled:
            known = await session.execute(
                select(KnownLot).where(
                    KnownLot.funpay_lot_id.in_([m.funpay_lot_id for m in enabled])
                )
            )
            known_rows = {row.funpay_lot_id: row for row in known.scalars().all()}

    if not enabled:
        return None

    desc = _norm_text(event.description)
    if desc:
        for mapping in enabled:
            label = _norm_text(mapping.label)
            if label and (label in desc or desc in label):
                log.warning(
                    f"FunPay order без lot_id сопоставлен по label: "
                    f"order={event.funpay_order_id}, label={mapping.label!r}, "
                    f"lot={mapping.funpay_lot_id}"
                )
                return mapping

    scored: list[tuple[int, Mapping]] = []
    if desc:
        for mapping in enabled:
            known_title = getattr(known_rows.get(mapping.funpay_lot_id), "title", None)
            score = _mapping_match_score(
                description=event.description,
                mapping=mapping,
                known_title=known_title,
            )
            if score > 0:
                scored.append((score, mapping))
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored:
            best_score, best_mapping = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else 0
            # Нужен явный отрыв, чтобы не выбрать случайный Apple-лот при
            # неоднозначном описании. При единственном совпадении score >= 20
            # достаточно: обычно это "apple + 2" или KnownLot title.
            if best_score >= 20 and best_score >= second_score + 10:
                log.warning(
                    f"FunPay order без lot_id сопоставлен по описанию: "
                    f"order={event.funpay_order_id}, lot={best_mapping.funpay_lot_id}, "
                    f"score={best_score}, second={second_score}, "
                    f"description={event.description!r}"
                )
                return best_mapping
            log.error(
                f"FunPay order без lot_id: описание похоже на несколько "
                f"маппингов, не выбираю автоматически. candidates="
                f"{[(score, mapping.funpay_lot_id) for score, mapping in scored[:5]]}, "
                f"description={event.description!r}"
            )
            return None

    if len(enabled) == 1:
        mapping = enabled[0]
        log.warning(
            f"FunPay order без lot_id: использую единственный активный "
            f"маппинг lot={mapping.funpay_lot_id}. "
            f"description={event.description!r}"
        )
        return mapping

    log.error(
        f"FunPay order без lot_id и не удалось однозначно выбрать маппинг: "
        f"enabled_mappings={len(enabled)}, description={event.description!r}"
    )
    return None


async def _resolve_chat_id(
    event: FunPayOrderEvent, funpay_client: FunPayClient | None, log
) -> int | None:
    if event.chat_id is not None:
        return event.chat_id
    if funpay_client is None or not event.buyer_username:
        return None

    def _lookup() -> int | None:
        account = funpay_client.account
        get_chat_by_name = getattr(account, "get_chat_by_name", None)
        if callable(get_chat_by_name):
            try:
                chat = get_chat_by_name(event.buyer_username, True)
                chat_id = getattr(chat, "id", None)
                return int(chat_id) if chat_id is not None else None
            except Exception:
                return None
        return None

    try:
        chat_id = await funpay_client._to_thread(_lookup)  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning(
            f"Не смог найти chat_id по buyer_username={event.buyer_username!r}: {exc}"
        )
        return None

    if chat_id is not None:
        log.info(
            f"Восстановил chat_id={chat_id} по buyer_username="
            f"{event.buyer_username!r}"
        )
    return chat_id


def _build_ns_fields(template_json: str | None, quantity: int) -> list[dict]:
    """
    Превратить ns_fields_template из mapping в готовый список fields
    для NS create_order. Поддерживается подстановка `@QUANTITY`.
    """
    if not template_json:
        return [{"key": "quantity", "value": quantity}]
    try:
        parsed = json.loads(template_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ns_fields_template не валидный JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("ns_fields_template должен быть JSON-объектом")
    fields: list[dict] = []
    for key, value in parsed.items():
        if isinstance(value, str) and value.strip() == "@QUANTITY":
            value = quantity
        fields.append({"key": key, "value": value})
    return fields


def _pins_from_order(order: Order) -> list:
    """Прочитать список пинов из Order.pins_json."""
    if not order.pins_json:
        return []
    try:
        data = json.loads(order.pins_json)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _order_age_seconds(order: Order, *, now: datetime | None = None) -> float:
    """Сколько секунд прошло от Order.created_at. naive UTC, как и весь проект."""
    current = now or datetime.utcnow()
    return max(0.0, (current - order.created_at).total_seconds())


def _is_hard_timeout(
    order: Order, settings: Settings, *, now: datetime | None = None
) -> bool:
    """True если истёк жёсткий лимит на полный цикл received→delivered."""
    limit = settings.order_delivery_hard_timeout_seconds
    if limit <= 0:
        return False
    return _order_age_seconds(order, now=now) >= limit


def _is_valid_uuid4(value: str | None) -> bool:
    """
    Истина если value — корректный UUID4-string в каноническом формате.

    NS API проверяет custom_id регуляркой uuid4 (8-4-4-4-12, version=4),
    поэтому нам недостаточно `uuid.UUID(value)` — он принимает и v5/v3/v1.
    UUID версия кодируется в 13-м hex-символе: для v4 это всегда `4`.
    """
    if not value:
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4


async def _ns_check_existing_order(
    ns_client: NSClient,
    custom_id: str,
    log,
) -> OrderInfo | None:
    """Аудит #1: idempotency-проверка существования NS-заказа.

    Возвращает OrderInfo если заказ уже существует в NS, иначе None.
    404 — заказа нет (можно безопасно создавать). Любая другая ошибка
    логируется и трактуется как «не знаем» → None (продолжим create/pay
    как обычно; deterministic custom_id защитит от UUID-дубля при retry).
    """
    try:
        return await ns_client.order_info(custom_id)
    except NSNotFoundError:
        return None
    except NSError as exc:
        log.warning(
            f"NS idempotency check для {custom_id} упал: {exc}; "
            "продолжаю обычным путём"
        )
        return None


async def _trigger_manual_hold(
    *,
    funpay_order_id: str,
    stage: str,
    reason: str,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    log,
) -> dict:
    """
    Перевести заказ в manual_hold по hard-timeout / NS-timeout.

    Что делает (в этом порядке):
      1) update_order(status='manual_hold', error=reason) — атомарно;
      2) пихает алерт в Telegram с кнопками retry/done/details;
      3) аварийно выключает FunPay-лот, чтобы новые покупатели не
         попадали на тот же узкий участок;
      4) возвращает stable-словарь {status: manual_hold, reason, ...}
         для возврата из process_funpay_order.

    Безопасно вызывать многократно: повторный manual_hold для уже
    held заказа просто перепишет error/timestamp, дублирующий
    Telegram-алерт оператор просто проигнорирует.
    """
    has_pins = False
    ns_custom_id: str | None = None
    buyer_username: str | None = None
    funpay_lot_id: int | None = None
    age_seconds = 0
    async with session_factory()() as session:
        order = await find_order_by_funpay_id(session, funpay_order_id)
        if order is not None:
            await update_order(
                session,
                order,
                status="manual_hold",
                error=f"{stage}: {reason}",
            )
            await session.commit()
            has_pins = bool(_pins_from_order(order))
            ns_custom_id = order.ns_custom_id
            buyer_username = order.buyer_username
            funpay_lot_id = order.funpay_lot_id
            age_seconds = int(_order_age_seconds(order))

    if telegram is not None:
        try:
            await telegram.manual_hold_required(
                funpay_order_id=funpay_order_id,
                stage=stage,
                age_seconds=age_seconds,
                buyer_username=buyer_username,
                ns_custom_id=ns_custom_id,
                has_pins=has_pins,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — diagnostics, alert не критичен
            log.warning(f"Не смог отправить manual_hold alert в Telegram: {exc}")

    await _emergency_disable_lot(
        funpay_lot_id,
        funpay_client,
        telegram,
        reason=f"manual_hold ({stage}): {reason}",
        log=log,
    )

    log.warning(
        f"manual_hold выставлен: stage={stage}, age={age_seconds}s, "
        f"ns_custom_id={ns_custom_id}, has_pins={has_pins}, reason={reason}"
    )
    return {
        "status": "manual_hold",
        "reason": reason,
        "stage": stage,
        "ns_custom_id": ns_custom_id,
        "has_pins": has_pins,
    }


async def _should_hold_delivery(
    session,
    order: Order,
    *,
    grace_seconds: int | None = None,
    manual_guard_seconds: int | None = None,
) -> bool:
    """
    True если перед доставкой нужно остановиться и отдать заказ оператору.

    Защита от двойной выдачи с ночным grace-period: покупатель может написать
    !помощь сразу после оплаты, но бот всё ещё имеет окно на нормальную
    автовыдачу. После окна заказ уходит оператору, чтобы не догнать ручную
    выдачу дублем.
    """
    if order.status == "manual_hold":
        return True
    if order.chat_id is None:
        return False
    from src.db.models import ChatState

    state = await session.get(ChatState, order.chat_id)
    if state is None:
        return False
    created_at = order.created_at
    # SQLite server_default в тестах иногда отдаёт naive datetime — сравниваем
    # naive UTC, как и остальной код в проекте.
    manual_at = getattr(state, "last_manual_message_at", None)
    if manual_at is not None:
        if manual_at >= created_at:
            return True
        if manual_guard_seconds is None:
            manual_guard_seconds = int(
                get_settings().order_manual_intervention_guard_seconds
            )
        if manual_guard_seconds <= 0:
            return True
        if created_at - manual_at <= timedelta(seconds=manual_guard_seconds):
            return True

    if state.last_help_request_at is None:
        return False
    if state.last_help_request_at < created_at:
        return False
    if grace_seconds is None:
        grace_seconds = int(get_settings().chat_help_auto_delivery_grace_seconds)
    if grace_seconds <= 0:
        return True
    return datetime.utcnow() >= created_at + timedelta(seconds=grace_seconds)


async def process_funpay_order(
    event: FunPayOrderEvent,
    *,
    settings: Settings | None = None,
    ns_client: NSClient | None = None,
    funpay_client: FunPayClient | None = None,
    telegram: TelegramNotifier | None = None,
    dry_run: bool | None = None,
    force_delivery: bool = False,
) -> dict:
    """
    Главный pipeline. Идемпотентный и сериализованный по funpay_order_id.
    """
    settings = settings or get_settings()
    if dry_run is None:
        dry_run = not settings.enable_real_actions

    log = logger.bind(funpay_order_id=event.funpay_order_id)

    async with _lock_for(event.funpay_order_id):
        return await _process_locked(
            event, settings, ns_client, funpay_client, telegram, dry_run, log,
            force_delivery=force_delivery,
        )


async def _process_locked(
    event: FunPayOrderEvent,
    settings: Settings,
    ns_client: NSClient | None,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    dry_run: bool,
    log,
    *,
    force_delivery: bool = False,
) -> dict:
    if event.chat_id is None:
        resolved_chat_id = await _resolve_chat_id(event, funpay_client, log)
        if resolved_chat_id is not None:
            event = replace(event, chat_id=resolved_chat_id)

    log.info(
        f"Обработка FunPay-заказа: lot={event.funpay_lot_id}, "
        f"qty={event.quantity}, buyer={event.buyer_username}, "
        f"chat={event.chat_id}, dry_run={dry_run}"
    )

    # ─── 1. Быстрый выход для уже доставленных ───
    async with session_factory()() as session:
        existing = await find_order_by_funpay_id(session, event.funpay_order_id)
    if existing is not None and existing.status == "delivered":
        log.info("Заказ уже доставлен, выхожу")
        return {
            "status": "delivered",
            "skipped": True,
            "ns_custom_id": existing.ns_custom_id,
        }
    if existing is not None and existing.status == "manual_hold" and not force_delivery:
        log.warning("Заказ на ручной проверке, автоматическую выдачу не продолжаю")
        return {
            "status": "manual_hold",
            "skipped": True,
            "reason": existing.error or "manual hold",
            "ns_custom_id": existing.ns_custom_id,
        }

    # ─── 2. Доставка только-pins (без обращения к NS) ───
    # Случай: pins_ready — деньги списаны, коды есть, но send_message
    # клиенту в прошлый раз упал. Просто повторяем доставку.
    if existing is not None and existing.status in ("pins_ready", "manual_hold"):
        pins = _pins_from_order(existing)
        if pins:
            log.warning(
                f"Повторная доставка {existing.status}: {len(pins)} код(а/ов)"
            )
            return await _deliver_pins(
                event, existing, pins, funpay_client, telegram, log,
                force_delivery=force_delivery,
                help_grace_seconds=int(settings.chat_help_auto_delivery_grace_seconds),
            )
        log.error("pins_ready без pins_json — пометить failed")
        await _mark_failed(
            existing.id, "pins_ready без сохранённых pins",
            telegram, event,
            funpay_client=funpay_client,
            funpay_lot_id=existing.funpay_lot_id,
            log=log,
        )
        return {"status": "failed", "reason": "pins_ready без pins"}

    # ─── 3. Маппинг ───
    mapping = await _resolve_mapping(event, log)
    if mapping is None or not mapping.enabled:
        reason = (
            f"нет маппинга для funpay_lot_id={event.funpay_lot_id} "
            f"(description={event.description!r})"
            if mapping is None
            else "маппинг выключен (enabled=false)"
        )
        log.error(reason)
        lot_to_disable = (
            mapping.funpay_lot_id if mapping is not None else event.funpay_lot_id
        )
        async with session_factory()() as session:
            order = existing or await create_order(
                session,
                funpay_order_id=event.funpay_order_id,
                funpay_lot_id=event.funpay_lot_id or 0,
                ns_service_id=0,
                buyer_username=event.buyer_username,
                buyer_user_id=event.buyer_user_id,
                chat_id=event.chat_id,
                quantity=event.quantity,
                funpay_price_rub=event.funpay_price_rub,
                description=event.description,
            )
            await update_order(session, order, status="failed", error=reason)
            await session.commit()
        if telegram is not None:
            await telegram.order_failure(
                funpay_order_id=event.funpay_order_id, reason=reason
            )
        await _emergency_disable_lot(
            lot_to_disable,
            funpay_client,
            telegram,
            reason=reason,
            log=log,
        )
        return {"status": "failed", "reason": reason}

    # ─── 4. Создаём/находим Order в БД ───
    effective_funpay_lot_id = (
        event.funpay_lot_id if event.funpay_lot_id > 0 else mapping.funpay_lot_id
    )
    async with session_factory()() as session:
        db_order = existing or await create_order(
            session,
            funpay_order_id=event.funpay_order_id,
            funpay_lot_id=effective_funpay_lot_id,
            ns_service_id=mapping.ns_service_id,
            buyer_username=event.buyer_username,
            buyer_user_id=event.buyer_user_id,
            chat_id=event.chat_id,
            quantity=event.quantity,
            funpay_price_rub=event.funpay_price_rub,
            description=event.description,
        )
        await session.commit()
        db_order_id = db_order.id
        order_status = db_order.status
        existing_ns_custom_id = db_order.ns_custom_id
        existing_ns_price_usd = db_order.ns_price_usd
        existing_age_seconds = _order_age_seconds(db_order)

    if not force_delivery and _is_hard_timeout(db_order, settings):
        return await _trigger_manual_hold(
            funpay_order_id=event.funpay_order_id,
            stage="before_ns_purchase",
            reason=(
                f"hard timeout: заказу {int(existing_age_seconds)}s, "
                f"лимит {settings.order_delivery_hard_timeout_seconds}s; "
                "автопокупка остановлена"
            ),
            funpay_client=funpay_client,
            telegram=telegram,
            log=log,
        )

    # Если продавец уже вмешался вручную после оплаты, не покупаем код в NS:
    # это дешевле и безопаснее, чем купить pins и остановиться только перед доставкой.
    if not force_delivery and order_status == "received" and existing_ns_custom_id is None:
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
            if await _should_hold_delivery(
                session,
                db_order,
                grace_seconds=int(settings.chat_help_auto_delivery_grace_seconds),
                manual_guard_seconds=int(settings.order_manual_intervention_guard_seconds),
            ):
                await update_order(
                    session,
                    db_order,
                    status="manual_hold",
                    error=(
                        "manual_hold: help/manual intervention before NS purchase; "
                        "автопокупка остановлена во избежание дубля"
                    ),
                )
                await session.commit()
                if telegram is not None:
                    await telegram.warning(
                        f"🛑 Заказ <code>{event.funpay_order_id}</code> "
                        "остановлен до покупки в NS: в чате было ручное вмешательство."
                    )
                return {
                    "status": "manual_hold",
                    "reason": "manual intervention before ns purchase",
                }

    # ─── 5. Приветствие в чате FunPay (один раз, при первом заходе) ───
    if (
        existing is None
        and funpay_client is not None
        and event.chat_id is not None
        and not dry_run
    ):
        try:
            await funpay_client.send_message(
                event.chat_id,
                templates.order_received(event.buyer_username or "друг"),
            )
        except Exception as exc:
            log.warning(f"Не отправил приветствие в чат: {exc}")

    own_ns = ns_client is None
    if own_ns:
        ns_client = NSClient()
        await ns_client.__aenter__()

    try:
        ns_custom_id = existing_ns_custom_id
        ns_price_usd = existing_ns_price_usd

        # ─── 6. NS create_order (если ещё не создан) ───
        if order_status in ("received",) or ns_custom_id is None:
            try:
                ns_fields = _build_ns_fields(
                    mapping.ns_fields_template, event.quantity
                )
            except ValueError as exc:
                error_text = f"Ошибка в шаблоне ns_fields_template: {exc}"
                log.error(error_text)
                await _mark_failed(
                    db_order_id, error_text, telegram, event,
                    funpay_client=funpay_client,
                    funpay_lot_id=effective_funpay_lot_id,
                    log=log,
                )
                return {"status": "failed", "reason": error_text}

            # Аудит #1: idempotency NS create_order.
            # 1) UUID4 custom_id, генерируется ОДИН раз и сразу пишется в БД
            #    — при retry мы возьмём этот же UUID и обратимся к ТОМУ ЖЕ
            #    NS-заказу, никакого UUID-дубля.
            #    NB: до 2026-05-25 здесь была детерминистическая схема
            #    `fp-{funpay_order_id}`, но NS обновил валидацию и теперь
            #    требует строго UUID4 ({400, custom_id must be a valid UUID4}).
            # 2) Intent marker: сохраняем UUID в БД ДО вызова NS, чтобы при
            #    crash до ответа retry уже знал, какой id проверять.
            # 3) Pre-check `order_info`: если предыдущий attempt успел дойти
            #    до NS — пропускаем create.
            if ns_custom_id is None or not _is_valid_uuid4(ns_custom_id):
                # Либо новый заказ, либо в БД остался legacy "fp-..." id
                # (с предыдущей версии кода). В обоих случаях генерим UUID4
                # и перезаписываем — старый "fp-..." не существует в NS,
                # обращение к нему по order_info даст 404 / валидационную
                # ошибку. Перегенерация безопасна, потому что create_order
                # для legacy-id всё равно бы провалился с 400.
                if ns_custom_id is not None:
                    log.warning(
                        f"legacy ns_custom_id={ns_custom_id!r} в БД не UUID4 — "
                        "перегенерирую"
                    )
                ns_custom_id = NSClient.new_custom_id()
            async with session_factory()() as session:
                db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
                assert db_order is not None
                if db_order.ns_custom_id != ns_custom_id:
                    await update_order(session, db_order, ns_custom_id=ns_custom_id)
                    await session.commit()

            pre_info = await _ns_check_existing_order(ns_client, ns_custom_id, log)
            if pre_info is not None:
                log.info(
                    f"NS idempotency: заказ {ns_custom_id} уже существует "
                    f"(status={pre_info.status_enum}), create пропускаю"
                )
                if pre_info.total_price is not None:
                    ns_price_usd = float(pre_info.total_price)
            else:
                try:
                    created = await ns_client.create_order(
                        service_id=mapping.ns_service_id,
                        fields=ns_fields,
                        custom_id=ns_custom_id,
                    )
                except NSError as exc:
                    error_text = f"NS create_order упал: {exc}"
                    log.error(error_text)
                    await _mark_failed(
                        db_order_id, error_text, telegram, event,
                        funpay_client=funpay_client,
                        funpay_lot_id=effective_funpay_lot_id,
                        log=log,
                    )
                    return {"status": "failed", "reason": error_text}
                ns_price_usd = float(created.total_to_pay)
            log.info(
                f"NS create_order: custom_id={ns_custom_id}, "
                f"к оплате={ns_price_usd:.4f} USD"
            )
            async with session_factory()() as session:
                db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
                assert db_order is not None
                await update_order(
                    session, db_order,
                    status="ns_created",
                    ns_custom_id=ns_custom_id,
                    ns_price_usd=ns_price_usd,
                )
                await session.commit()
            order_status = "ns_created"

        if dry_run:
            log.warning(
                f"DRY-RUN: ENABLE_REAL_ACTIONS=false → НЕ оплачиваю NS-заказ "
                f"({ns_custom_id}). NS сам отменит его через ~10 минут."
            )
            return {
                "status": "ns_created",
                "ns_custom_id": ns_custom_id,
                "ns_price_usd": ns_price_usd,
                "dry_run": True,
            }

        # ─── 7. NS pay_order (если ещё не оплачен) ───
        pins: list = []
        if order_status == "ns_created":
            # Аудит #1: idempotency pay_order. Pre-check `order_info`:
            # если предыдущий pay уже дошёл до NS (status != CREATED),
            # повторный pay не нужен — переиспользуем существующий статус.
            # Это предотвращает потенциальное двойное списание.
            pre_info = await _ns_check_existing_order(ns_client, ns_custom_id, log)
            pre_status = pre_info.status_enum if pre_info is not None else None

            if pre_status in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
                msg = pre_info.status_message if pre_info else ""
                error_text = f"NS вернул возврат/отмену (idempotency check): {msg}"
                log.error(error_text)
                await _mark_failed(
                    db_order_id, error_text, telegram, event,
                    funpay_client=funpay_client,
                    funpay_lot_id=effective_funpay_lot_id,
                    log=log,
                )
                return {"status": "failed", "reason": error_text}

            if pre_status in (OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED):
                log.info(
                    f"NS idempotency: pay_order пропускаю — заказ "
                    f"{ns_custom_id} уже в статусе {pre_status.name}"
                )
                pins = list(pre_info.pins or []) if pre_info else []
            else:
                try:
                    pay_resp = await ns_client.pay_order(ns_custom_id)
                except NSInsufficientFunds as exc:
                    error_text = f"Недостаточно средств на NS: balance={exc.balance}"
                    log.error(error_text)
                    await _mark_failed(
                        db_order_id, error_text, telegram, event,
                        funpay_client=funpay_client,
                        funpay_lot_id=effective_funpay_lot_id,
                        log=log,
                    )
                    return {"status": "failed", "reason": error_text}
                except NSError as exc:
                    error_text = f"NS pay_order упал: {exc}"
                    log.error(error_text)
                    await _mark_failed(
                        db_order_id, error_text, telegram, event,
                        funpay_client=funpay_client,
                        funpay_lot_id=effective_funpay_lot_id,
                        log=log,
                    )
                    return {"status": "failed", "reason": error_text}
                log.info(f"NS pay_order: status={pay_resp.status}")
                pins = list(pay_resp.pins or [])
            async with session_factory()() as session:
                db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
                assert db_order is not None
                await update_order(session, db_order, status="ns_paid")
                await session.commit()
            order_status = "ns_paid"

        # ─── 8. Получаем pins (если ещё не получили) ───
        if order_status == "ns_paid" and not pins:
            # wait_order_completion внутри сам поллит NS до своего timeout'a
            # (NS_ORDER_TIMEOUT_SECONDS). Мы дополнительно усекаем его до
            # остатка до hard-timeout, чтобы не уходить за общий лимит
            # цикла received→delivered. min_wait = 10s, чтобы хотя бы один
            # poll-цикл успел отработать; иначе сразу manual_hold.
            wait_timeout: float | None = None
            if settings.order_delivery_hard_timeout_seconds > 0:
                async with session_factory()() as session:
                    fresh = await find_order_by_funpay_id(
                        session, event.funpay_order_id
                    )
                assert fresh is not None
                age = _order_age_seconds(fresh)
                remaining = (
                    settings.order_delivery_hard_timeout_seconds - age
                )
                if remaining <= 10:
                    return await _trigger_manual_hold(
                        funpay_order_id=event.funpay_order_id,
                        stage="ns_wait_completion",
                        reason=(
                            f"hard timeout до старта ожидания pins: "
                            f"age={int(age)}s, "
                            f"лимит={settings.order_delivery_hard_timeout_seconds}s"
                        ),
                        funpay_client=funpay_client,
                        telegram=telegram,
                        log=log,
                    )
                wait_timeout = min(
                    float(settings.ns_order_timeout_seconds), remaining
                )
            try:
                info = await ns_client.wait_order_completion(
                    ns_custom_id, timeout_seconds=wait_timeout
                )
            except NSOrderTimeoutError as exc:
                # Деньги уже списаны в NS, но pins не пришли вовремя.
                # Это ровно тот сценарий, где нужен оператор: проверить
                # NS-кабинет/саппорт и решить, выдавать ли вручную.
                reason = f"NS не выдал коды за тайм-аут: {exc}"
                log.error(reason)
                return await _trigger_manual_hold(
                    funpay_order_id=event.funpay_order_id,
                    stage="ns_wait_completion",
                    reason=reason,
                    funpay_client=funpay_client,
                    telegram=telegram,
                    log=log,
                )
            except NSError as exc:
                # Аудит #6: NSAPIError (429 после retry-исчерпания, 5xx,
                # 4xx и т.п.) ПОСЛЕ pay_order. Деньги уже списаны, pins
                # не получены — оператор должен решить через Telegram.
                # До фикса: исключение вылетало наверх, статус оставался
                # ns_paid, никакого алерта.
                reason = f"NS вернул ошибку при ожидании pins: {exc}"
                log.error(reason)
                return await _trigger_manual_hold(
                    funpay_order_id=event.funpay_order_id,
                    stage="ns_wait_completion",
                    reason=reason,
                    funpay_client=funpay_client,
                    telegram=telegram,
                    log=log,
                )
            if info.status_enum == OrderStatus.COMPLETED and info.pins:
                pins = list(info.pins)
            elif info.status_enum in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
                error_text = f"NS вернул возврат/отмену: {info.status_message}"
                log.error(error_text)
                await _mark_failed(
                    db_order_id, error_text, telegram, event,
                    funpay_client=funpay_client,
                    funpay_lot_id=effective_funpay_lot_id,
                    log=log,
                )
                return {"status": "failed", "reason": error_text}

        if not pins:
            error_text = "NS заказ завершился, но pins пустой"
            log.error(error_text)
            await _mark_failed(
                db_order_id, error_text, telegram, event,
                funpay_client=funpay_client,
                funpay_lot_id=effective_funpay_lot_id,
                log=log,
            )
            return {"status": "failed", "reason": error_text}

        # Сохраняем pins И помечаем pins_ready — это критическая точка:
        # дальше деньги уже не вернуть, надо обязательно доставить.
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
            assert db_order is not None
            # Hard-timeout сразу после получения pins: pins на руках,
            # но мы переползли общий лимит. Сначала сохраняем pins
            # отдельным flush'ом, чтобы они не потерялись, а статус и
            # alert ставит _trigger_manual_hold ниже.
            if not force_delivery and _is_hard_timeout(db_order, settings):
                age_now = int(_order_age_seconds(db_order))
                await update_order(session, db_order, pins=pins)
                await session.commit()
                return await _trigger_manual_hold(
                    funpay_order_id=event.funpay_order_id,
                    stage="post_pins_pre_delivery",
                    reason=(
                        f"hard timeout с pins на руках: age={age_now}s, "
                        f"лимит={settings.order_delivery_hard_timeout_seconds}s; "
                        f"pins сохранены, нажми Retry для доставки"
                    ),
                    funpay_client=funpay_client,
                    telegram=telegram,
                    log=log,
                )
            if (
                not force_delivery
                and await _should_hold_delivery(
                    session,
                    db_order,
                    grace_seconds=int(settings.chat_help_auto_delivery_grace_seconds),
                    manual_guard_seconds=int(
                        settings.order_manual_intervention_guard_seconds
                    ),
                )
            ):
                await update_order(
                    session,
                    db_order,
                    pins=pins,
                    status="manual_hold",
                    error=(
                        "manual_hold: help/manual intervention before delivery; "
                        "pins сохранены, автоматическая выдача остановлена во избежание дубля"
                    ),
                )
                await session.commit()
                if telegram is not None:
                    await telegram.warning(
                        f"🛑 Заказ <code>{event.funpay_order_id}</code> "
                        "поставлен на ручную проверку после help/manual intervention. "
                        "Pins сохранены, покупателю автоматически не отправлены."
                    )
                return {
                    "status": "manual_hold",
                    "ns_custom_id": ns_custom_id,
                    "pins_count": len(pins),
                    "reason": "help/manual intervention before delivery",
                }
            await update_order(session, db_order, status="pins_ready", pins=pins)
            await session.commit()
        log.info(f"NS pins получены ({len(pins)} шт), статус pins_ready")

        # ─── 9. Доставка клиенту ───
        async with session_factory()() as session:
            db_order = await find_order_by_funpay_id(session, event.funpay_order_id)
        assert db_order is not None
        return await _deliver_pins(
            event, db_order, pins, funpay_client, telegram, log,
            ns_custom_id=ns_custom_id, ns_price_usd=ns_price_usd,
            force_delivery=force_delivery,
            help_grace_seconds=int(settings.chat_help_auto_delivery_grace_seconds),
        )

    finally:
        if own_ns and ns_client is not None:
            await ns_client.__aexit__(None, None, None)


async def _deliver_pins(
    event: FunPayOrderEvent,
    db_order: Order,
    pins: list,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    log,
    *,
    ns_custom_id: str | None = None,
    ns_price_usd: float | None = None,
    force_delivery: bool = False,
    help_grace_seconds: int | None = None,
) -> dict:
    """
    Шаг доставки: отправляет коды в чат FunPay, обновляет статус.
    На вход подаются уже сохранённые pins. Если FunPay упал — статус
    останется pins_ready, повторим в следующий вход.
    """
    ns_custom_id = ns_custom_id or db_order.ns_custom_id
    ns_price_usd = ns_price_usd if ns_price_usd is not None else db_order.ns_price_usd

    async with session_factory()() as session:
        latest = await find_order_by_funpay_id(session, event.funpay_order_id)
        # ── Защита от гонки с оператором ──
        # Между моментом, когда мы вошли в _deliver_pins, и моментом
        # send_message, оператор мог в Telegram нажать «✅ Выдано вручную»
        # (или «Retry» с уже delivered'нутыми pins). В этом случае
        # автоматическая повторная отправка = дубль = потеря денег.
        # Гард срабатывает ДАЖЕ при force_delivery: если оператор уже
        # пометил выдано вручную, никакой Retry не должен переотправить.
        if latest is not None and latest.status == "delivered":
            log.warning(
                "Заказ уже delivered (вероятно оператор подтвердил вручную) — "
                "не отправляю pins повторно (force_delivery="
                f"{force_delivery})"
            )
            return {
                "status": "delivered",
                "ns_custom_id": ns_custom_id,
                "pins_count": len(pins),
                "skipped": True,
                "reason": "already delivered by operator",
            }
        if (
            latest is not None
            and not force_delivery
            and await _should_hold_delivery(
                session,
                latest,
                grace_seconds=help_grace_seconds,
                manual_guard_seconds=int(
                    get_settings().order_manual_intervention_guard_seconds
                ),
            )
        ):
            await update_order(
                session,
                latest,
                status="manual_hold",
                error=(
                    latest.error
                    or "manual_hold: help/manual intervention; автодоставка остановлена"
                ),
            )
            await session.commit()
            if telegram is not None:
                await telegram.warning(
                    f"🛑 Не отправляю pins по заказу "
                    f"<code>{event.funpay_order_id}</code>: активна ручная проверка."
                )
            return {
                "status": "manual_hold",
                "ns_custom_id": ns_custom_id,
                "pins_count": len(pins),
                "reason": "manual hold",
            }

    delivery_text = templates.delivery(event.buyer_username or "друг", pins)

    if funpay_client is None or event.chat_id is None:
        log.warning(
            "FunPay-клиент или chat_id отсутствуют — доставка отложена; "
            "статус остаётся pins_ready"
        )
        await _emergency_disable_lot(
            db_order.funpay_lot_id,
            funpay_client,
            telegram,
            reason="pins_ready: нет FunPay-клиента или chat_id для доставки",
            log=log,
        )
        return {
            "status": "pins_ready",
            "ns_custom_id": ns_custom_id,
            "pins_count": len(pins),
            "reason": "no funpay client or chat_id",
        }

    # Аудит #3: двухфазная доставка. ДО send_message — статус `delivering`.
    # Это intent-marker «попытка отправки в процессе». Если процесс упадёт
    # между success send_message и commit'ом delivered, статус останется
    # delivering, и reconciler НЕ повторит отправку автоматически
    # (риск двойной выдачи) — переведёт в manual_hold для оператора.
    async with session_factory()() as session:
        order = await find_order_by_funpay_id(session, event.funpay_order_id)
        if order is not None:
            await update_order(session, order, status="delivering")
            await session.commit()

    try:
        await funpay_client.send_message(event.chat_id, delivery_text)
    except Exception as exc:
        # send_message бросил — сообщение НЕ ушло. Безопасно откатить
        # на pins_ready, чтобы reconciler/Retry могли попробовать ещё раз.
        log.error(f"Доставка в чат FunPay упала: {exc}; откат на pins_ready")
        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, event.funpay_order_id)
            if order is not None and order.status == "delivering":
                await update_order(session, order, status="pins_ready")
                await session.commit()
        await _emergency_disable_lot(
            db_order.funpay_lot_id,
            funpay_client,
            telegram,
            reason=f"pins_ready: доставка в чат FunPay упала: {exc}",
            log=log,
        )
        if telegram is not None:
            await telegram.error(
                f"⚠ Не доставил pins в чат FunPay (order "
                f"<code>{event.funpay_order_id}</code>): <code>{exc}</code>. "
                f"Статус: pins_ready. Бот повторит доставку при следующей "
                f"возможности."
            )
        return {
            "status": "pins_ready",
            "ns_custom_id": ns_custom_id,
            "pins_count": len(pins),
            "delivery_error": str(exc),
        }

    log.success(f"Доставил {len(pins)} код(а/ов) в чат {event.chat_id}")
    fx_rate_at_sale: float | None = None
    profit_rub: float | None = None
    profit_margin_percent: float | None = None
    try:
        settings = get_settings()
        fx_rate_at_sale = await get_usd_rub_rate(settings)
        estimated = estimate_profit_rub(
            event.funpay_price_rub,
            ns_price_usd,
            fx_rate_at_sale,
            withdrawal_fee_percent=settings.funpay_withdrawal_fee_percent,
        )
        if estimated is not None:
            _, _, profit_rub, profit_margin_percent = estimated
    except Exception as exc:
        log.warning(f"Не смог посчитать точную прибыль заказа: {exc}")
    async with session_factory()() as session:
        order = await find_order_by_funpay_id(session, event.funpay_order_id)
        assert order is not None
        await update_order(
            session,
            order,
            status="delivered",
            fx_rate_at_sale=fx_rate_at_sale,
            profit_rub=profit_rub,
            profit_margin_percent=profit_margin_percent,
        )
        # Инвалидация diff-cache. FunPay при продаже САМ списывает сток
        # (100→97), наш target = min(NS, cap) = 100 не меняется, поэтому
        # без инвалидации diff-cache видит совпадение и пропускает sync
        # — FunPay-сток так и торчит на 97 до истечения TTL. Сбрасываем
        # last_synced_at, чтобы следующий sync-цикл (≤30с) пошёл через
        # реальный FunPay GET и поднял сток обратно к target.
        #
        # ВАЖНО: используем `order.funpay_lot_id`, а НЕ `event.funpay_lot_id`.
        # Для заказов, пришедших через chat handler / order discovery,
        # event.funpay_lot_id может быть 0 (FunPayAPI часто не отдаёт
        # lot_id в OrderShortcut). В таком случае мы матчили лот по
        # описанию в _resolve_mapping, и сохранили в БД именно
        # эффективный lot_id из mapping'а — его и используем.
        effective_lot_id = order.funpay_lot_id or event.funpay_lot_id
        if effective_lot_id and effective_lot_id > 0:
            try:
                await invalidate_mapping_cache_for_funpay_lot(
                    session, funpay_lot_id=effective_lot_id
                )
            except Exception as exc:
                log.warning(
                    f"invalidate_mapping_cache_for_funpay_lot упал "
                    f"(lot={effective_lot_id}): {exc}"
                )
        await session.commit()

    if telegram is not None:
        await telegram.order_success(
            funpay_order_id=event.funpay_order_id,
            ns_custom_id=ns_custom_id,
            ns_price_usd=ns_price_usd,
            funpay_price_rub=event.funpay_price_rub,
            buyer_username=event.buyer_username,
        )

    return {
        "status": "delivered",
        "ns_custom_id": ns_custom_id,
        "ns_price_usd": ns_price_usd,
        "pins_count": len(pins),
    }


async def _mark_failed(
    db_order_id: int | None,
    reason: str,
    telegram: TelegramNotifier | None,
    event: FunPayOrderEvent,
    *,
    funpay_client: FunPayClient | None = None,
    funpay_lot_id: int | None = None,
    log=None,
) -> None:
    if db_order_id is not None:
        async with session_factory()() as session:
            order = await find_order_by_funpay_id(session, event.funpay_order_id)
            if order is not None:
                await update_order(session, order, status="failed", error=reason)
                await session.commit()
                if funpay_lot_id is None:
                    funpay_lot_id = order.funpay_lot_id
    if telegram is not None:
        await telegram.order_failure(
            funpay_order_id=event.funpay_order_id, reason=reason
        )
    await _emergency_disable_lot(
        funpay_lot_id if funpay_lot_id is not None else event.funpay_lot_id,
        funpay_client,
        telegram,
        reason=reason,
        log=log,
    )


async def _emergency_disable_lot(
    funpay_lot_id: int | None,
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
    *,
    reason: str,
    log=None,
) -> bool:
    """
    Fail-safe: если автоматическая выдача по лоту сломалась, лот надо
    немедленно убрать из продажи, чтобы следующие покупатели не продолжили
    покупать проблемный товар.
    """
    if funpay_lot_id is None or funpay_lot_id <= 0:
        if log is not None:
            log.warning(
                f"Не могу аварийно выключить лот: неизвестный lot_id "
                f"(reason={reason})"
            )
        return False
    if funpay_client is None:
        if log is not None:
            log.warning(
                f"Не могу аварийно выключить лот {funpay_lot_id}: "
                "FunPay-клиент отсутствует"
            )
        return False

    funpay_disabled = False
    funpay_error: str | None = None

    try:
        lot_fields = await funpay_client.get_lot_fields(funpay_lot_id)
        if hasattr(lot_fields, "active"):
            lot_fields.active = False
        if hasattr(lot_fields, "amount"):
            lot_fields.amount = 0
        result = await funpay_client.save_lot(lot_fields)
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(result)
        funpay_disabled = True
    except Exception as exc:
        funpay_error = str(exc)
        if log is not None:
            log.opt(exception=exc).error(
                f"Не смог аварийно выключить FunPay-лот {funpay_lot_id}: {exc}"
            )

    # Аудит #8: даже если save_lot на FunPay упал, отключаем mapping в БД.
    # Иначе sync_stock на следующем цикле увидит mapping.enabled=True и
    # «починит» лот обратно — проблемный лот продолжит продаваться,
    # копируя новые failed-заказы.
    mapping_disabled_in_db = False
    try:
        async with session_factory()() as session:
            mapping = (
                await session.execute(
                    select(Mapping).where(Mapping.funpay_lot_id == funpay_lot_id)
                )
            ).scalar_one_or_none()
            if mapping is not None and mapping.enabled:
                mapping.enabled = False
                await session.commit()
                mapping_disabled_in_db = True
    except Exception as exc:
        if log is not None:
            log.opt(exception=exc).error(
                f"Не смог отключить mapping в БД для FunPay-лота "
                f"{funpay_lot_id}: {exc}"
            )

    if not funpay_disabled:
        if telegram is not None:
            extra = (
                "Локальный mapping отключён — sync лот обратно не включит, "
                "но вручную через FunPay UI лот всё ещё активен."
                if mapping_disabled_in_db
                else "ВНИМАНИЕ: и mapping в БД отключить не удалось."
            )
            await telegram.error(
                f"🚨 Не смог аварийно выключить FunPay-лот "
                f"<code>{funpay_lot_id}</code>: "
                f"<code>{(funpay_error or '?')[:300]}</code>. {extra}"
            )
        return False

    if log is not None:
        log.warning(
            f"FunPay-лот {funpay_lot_id} аварийно выключен после ошибки: {reason}"
        )
    if telegram is not None:
        await telegram.warning(
            f"FunPay-лот <code>{funpay_lot_id}</code> аварийно выключен. "
            f"Локальный маппинг тоже отключён, чтобы sync не включил лот "
            f"обратно до ручной проверки. Причина: <code>{reason[:300]}</code>"
        )
    return True
