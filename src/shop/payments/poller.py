"""
Фоновый воркер: пуллит paid-invoice'ы CryptoBot и применяет их идемпотентно.

Дизайн:
  - Каждые `cryptobot_polling_seconds` секунд вызываем
    poll_cryptobot_once(); регистрация в scheduler'е — в src/main.py.
  - Polling — основной механизм (webhook опционален; без nginx/SSL он
    в принципе невозможен). На typical-нагрузке (10-50 платежей в день)
    polling каждые 30с даёт 2880 запросов в сутки к Crypto Pay — это
    далеко от лимитов (CryptoBot не публикует жёсткие, но обычно
    >10 req/sec разрешено).
  - getInvoices(status="paid") возвращает ВСЕ paid invoice'ы (без TTL
    в API), но мы фильтруем по UNIQUE provider_invoice_id в БД, поэтому
    дубли не страшны: пройдут через apply_paid_invoice как no-op.

Граничные случаи:
  - сеть упала → CryptoBotError / httpx.HTTPError → логируем, возвращаем
    stats и ждём следующего тика;
  - частичная оплата → CryptoBot такого не делает (только paid либо
    expired), но если бы делал, мы бы видели status != 'paid' и пропустили;
  - юзер заплатил больше / меньше → CryptoBot всё равно показывает
    fiat amount как заказывали (мы фиксировали в createInvoice);
  - старый паылод в БД без topup_user_id → apply_paid_invoice залогирует
    варнинг и НЕ начислит — деньги «зависнут» в shop_payments.paid.
    Юзер увидит в /support и владелец увидит в логах.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from loguru import logger

from src.config import Settings
from src.db.session import session_factory
from src.shop.payments.cryptobot import (
    CryptoBotClient,
    CryptoBotError,
    Invoice,
)
from src.shop.repo import apply_paid_invoice


@dataclass(frozen=True)
class PollResult:
    """Сводка одного прогона polling-воркера."""
    checked: int    # сколько paid invoice'ов вернул CryptoBot
    applied: int    # сколько мы реально начислили (новых)
    skipped: int    # сколько уже было paid в нашей БД (повторы)
    errors: int     # сколько прошли с исключением (мы их логируем)


async def poll_cryptobot_once(
    *,
    settings: Settings,
    client: CryptoBotClient | None = None,
    notifier=None,  # Callable[[int_telegram_id, str], Awaitable[None]] | None
) -> PollResult:
    """
    Один прогон polling-воркера.

    Возвращает PollResult с метриками. Никогда не падает наружу —
    все исключения CryptoBot/SQLAlchemy логируются.

    notifier (опционально): callback (telegram_id, text) для отправки
    юзеру сообщения «✅ +500 ₽ зачислено». Если не передан — просто
    обновляем БД (юзер увидит при следующем открытии /balance).
    """
    api_token = (
        settings.cryptobot_api_token.get_secret_value()
        if settings.cryptobot_api_token else None
    )
    if not api_token:
        return PollResult(0, 0, 0, 0)

    cli = client or CryptoBotClient(
        api_token=api_token,
        testnet=settings.cryptobot_testnet,
    )

    try:
        paid_invoices = await cli.get_invoices(status="paid", count=100)
    except (CryptoBotError, Exception) as exc:  # noqa: BLE001
        logger.warning(f"cryptobot poll: getInvoices failed: {exc}")
        return PollResult(0, 0, 0, 1)

    applied, skipped, errors = 0, 0, 0
    notify_tasks: list[tuple[int, str]] = []

    factory = session_factory()
    async with factory() as session:
        for inv in paid_invoices:
            try:
                # amount у CryptoBot — Decimal в RUB, мы храним в копейках
                kopecks = _decimal_rub_to_kopecks(inv.amount)
                payment, was_just_applied = await apply_paid_invoice(
                    session,
                    provider="cryptobot",
                    provider_invoice_id=str(inv.invoice_id),
                    paid_amount_kopecks=kopecks,
                    paid_at=_parse_iso_or_none(inv.paid_at),
                    raw_payload_json=json.dumps(inv.raw, ensure_ascii=False),
                )
                if payment is None:
                    # Мы не создавали такой invoice (мб старый от другого
                    # окружения или ручной createInvoice). Игнорим, но
                    # отмечаем в метрике.
                    skipped += 1
                    continue
                if was_just_applied:
                    applied += 1
                    # Извлечь telegram_id юзера для уведомления
                    if notifier is not None and payment.raw_payload_json:
                        tg_id = _extract_telegram_id_for_notify(
                            payment.raw_payload_json, session
                        )
                        if tg_id is not None:
                            notify_tasks.append((
                                tg_id,
                                _format_paid_message(kopecks, inv.invoice_id),
                            ))
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"cryptobot poll: failed to apply invoice {inv.invoice_id}: {exc}"
                )
                errors += 1
        await session.commit()

    # Шлём уведомления ПОСЛЕ commit'а — чтобы юзер увидел свежий баланс
    if notifier is not None and notify_tasks:
        for tg_id, text in notify_tasks:
            try:
                await notifier(tg_id, text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"cryptobot poll: notify {tg_id} failed: {exc}")

    # Heartbeat-лог: всегда пишем результат прогона на INFO.
    # Это полезно для диагностики на VPS: пока нет ни одного пополнения,
    # tail -f журнала покажет тихую строку «checked=0 applied=0», и сразу
    # видно что воркер живой и токен валиден. Без этого лога мы не могли
    # отличить «работает но нет платежей» от «не запустился вообще».
    # Поток событий низкочастотный (раз в 30с) — спама не будет.
    logger.info(
        f"cryptobot poll: checked={len(paid_invoices)} "
        f"applied={applied} skipped={skipped} errors={errors}"
    )
    return PollResult(
        checked=len(paid_invoices),
        applied=applied,
        skipped=skipped,
        errors=errors,
    )


def _decimal_rub_to_kopecks(amount_rub: Decimal) -> int:
    """500.50 RUB → 50050 kopecks. Округляем bank's rounding (ROUND_HALF_EVEN)."""
    # Сначала умножим на 100, потом quantize до целого
    scaled = amount_rub * Decimal(100)
    return int(scaled.to_integral_value(rounding="ROUND_HALF_EVEN"))


def _parse_iso_or_none(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # CryptoBot: "2026-05-25T18:00:00.000Z"
        # datetime.fromisoformat не понимает 'Z' до 3.11, заменим
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_telegram_id_for_notify(
    raw_payload_json: str, session,
) -> int | None:
    """
    raw_payload_json содержит topup_user_id (наш shop_users.id).
    Нам нужно telegram_user_id для отправки сообщения.
    Делаем sync-lookup, потому что используется только внутри poll'а.

    NB: метод async НЕ нужен здесь — мы вернём int и пусть caller сам
    решает, отправлять или нет. Но т.к. session async, делаем через
    отдельный helper в repo. Здесь — заглушка, реализуем при подключении.

    На данном этапе мы ничего не возвращаем — notifier-логика будет
    окончательно подключена в src/main.py с прямым lookup юзера.
    """
    try:
        payload = json.loads(raw_payload_json)
        return int(payload.get("notify_telegram_id"))
    except (ValueError, TypeError):
        return None


def _format_paid_message(kopecks: int, invoice_id: int) -> str:
    rub = kopecks / 100
    if rub == int(rub):
        amount_str = f"{int(rub)} ₽"
    else:
        amount_str = f"{rub:.2f} ₽"
    return (
        f"✅ <b>Оплата получена</b>\n\n"
        f"На ваш баланс зачислено <b>{amount_str}</b>.\n"
        f"Invoice: <code>#{invoice_id}</code>\n\n"
        f"Спасибо за пополнение! Можно сразу делать покупки в каталоге."
    )
