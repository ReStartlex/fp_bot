"""
Обнаружение новых FunPay-лотов.

Идея: периодически вытягиваем все лоты нашего FunPay-аккаунта и
сравниваем со списком известных (`KnownLot`). Если появился лот,
которого мы раньше не видели — пушим алерт в Telegram, чтобы
пользователь мог быстро привязать его к NS-сервису.

Также по тому же таймеру обновляется `last_seen_at` и `title` —
получаем простое представление о том, какие лоты сейчас активны.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import select

from src.alerts.telegram import TelegramNotifier
from src.db.models import KnownLot, Mapping
from src.db.session import session_factory
from src.funpay.client import FunPayClient


def _lot_id(lot: Any) -> int | None:
    for attr in ("id", "lot_id", "ID"):
        v = getattr(lot, attr, None)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                continue
    return None


def _lot_title(lot: Any) -> str | None:
    for attr in ("description", "title", "name", "summary"):
        v = getattr(lot, attr, None)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    return None


async def discover_new_lots(
    funpay_client: FunPayClient | None,
    telegram: TelegramNotifier | None,
) -> dict[str, int]:
    """
    Один прогон discovery.
    Возвращает {"seen": N, "new": K, "notified": M}.
    """
    if funpay_client is None:
        logger.debug("discover_new_lots: FunPay не подключён, пропускаю")
        return {"seen": 0, "new": 0, "notified": 0}

    try:
        lots = await funpay_client.get_my_lots()
    except Exception as exc:
        logger.warning(f"discover_new_lots: не удалось получить лоты FunPay: {exc}")
        return {"seen": 0, "new": 0, "notified": 0}

    if not lots:
        logger.debug("discover_new_lots: get_my_lots() вернул пусто")
        return {"seen": 0, "new": 0, "notified": 0}

    seen = 0
    new_lots: list[tuple[int, str | None]] = []
    now = datetime.utcnow()
    async with session_factory()() as session:
        existing_ids = {
            r for r in (
                await session.execute(select(KnownLot.funpay_lot_id))
            ).scalars().all()
        }
        mapped_ids = {
            r for r in (
                await session.execute(select(Mapping.funpay_lot_id))
            ).scalars().all()
        }

        for lot in lots:
            lid = _lot_id(lot)
            if lid is None or lid <= 0:
                continue
            seen += 1
            title = _lot_title(lot)
            if lid in existing_ids:
                row = await session.get(KnownLot, lid)
                if row is not None:
                    row.last_seen_at = now
                    if title and row.title != title:
                        row.title = title
                continue
            row = KnownLot(
                funpay_lot_id=lid,
                title=title,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(row)
            if lid not in mapped_ids:
                new_lots.append((lid, title))

        await session.commit()

    notified = 0
    if new_lots and telegram is not None:
        for lid, title in new_lots:
            try:
                await telegram.new_lot_discovered(lid, title)
                notified += 1
                async with session_factory()() as session:
                    row = await session.get(KnownLot, lid)
                    if row is not None:
                        row.notified_at = datetime.utcnow()
                        await session.commit()
            except Exception as exc:
                logger.warning(f"Не пушнул алерт о новом лоте {lid}: {exc}")

    if new_lots:
        logger.info(
            f"discover_new_lots: всего лотов FunPay {seen}, "
            f"новых без маппинга {len(new_lots)}, нотификаций {notified}"
        )
    else:
        logger.debug(
            f"discover_new_lots: всего лотов FunPay {seen}, новых нет"
        )
    return {"seen": seen, "new": len(new_lots), "notified": notified}
