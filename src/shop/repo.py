"""
Shop-репозиторий: атомарные операции, которые нельзя ломать.

Главное правило: любая мутация ShopUser.balance_kopecks ИДЁТ ЧЕРЕЗ
`apply_balance_change()`, которая создаёт парную запись в ShopBalanceLedger
в той же транзакции. Это даёт нам инвариант
`sum(ledger.change for user) == user.balance` навсегда.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ShopBalanceLedger, ShopReferral, ShopUser


async def get_or_create_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_username: str | None = None,
    first_name: str | None = None,
    language_code: str | None = None,
) -> tuple[ShopUser, bool]:
    """
    Возвращает (user, is_new). Idempotent: повторный вызов с тем же
    telegram_user_id вернёт того же юзера, обновив last_seen + поля профиля
    (имя/username/язык могли поменяться в Telegram).
    """
    res = await session.execute(
        select(ShopUser).where(ShopUser.telegram_user_id == telegram_user_id)
    )
    user = res.scalar_one_or_none()
    if user is not None:
        changed = False
        if telegram_username is not None and user.telegram_username != telegram_username:
            user.telegram_username = telegram_username
            changed = True
        if first_name is not None and user.first_name != first_name:
            user.first_name = first_name
            changed = True
        if language_code is not None and user.language_code != language_code:
            user.language_code = language_code
            changed = True
        # last_seen_at обновляется через onupdate=func.now(), но это
        # триггерится только если хоть одно поле изменилось. Принудительный
        # touch — мини-update last_seen_at независимо от других полей.
        user.last_seen_at = datetime.utcnow()
        if changed:
            logger.debug(
                f"shop user {telegram_user_id} profile updated"
            )
        await session.flush()
        return user, False

    user = ShopUser(
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        first_name=first_name,
        language_code=language_code,
        balance_kopecks=0,
    )
    session.add(user)
    await session.flush()
    logger.info(
        f"shop: new user tg={telegram_user_id} name={first_name!r} "
        f"id={user.id}"
    )
    return user, True


async def attach_referral(
    session: AsyncSession,
    *,
    referrer_user_id: int,
    referred_user_id: int,
) -> ShopReferral | None:
    """
    Привязка реферала к inviter'у. Возвращает запись если создалась впервые,
    None если реферал уже привязан (даже к другому inviter'у) — на этом
    уровне ничего не делаем, просто игнорируем.

    Защита от self-referral: inviter != invited.
    """
    if referrer_user_id == referred_user_id:
        logger.warning(
            f"shop: skip self-referral attempt user_id={referrer_user_id}"
        )
        return None

    # UNIQUE на referred_user_id даёт нам идемпотентность на уровне БД.
    # Сначала смотрим в БД — это быстрее и понятнее, чем ловить IntegrityError.
    res = await session.execute(
        select(ShopReferral).where(ShopReferral.referred_user_id == referred_user_id)
    )
    existing = res.scalar_one_or_none()
    if existing is not None:
        return None

    ref = ShopReferral(
        referrer_user_id=referrer_user_id,
        referred_user_id=referred_user_id,
    )
    session.add(ref)

    # Дублируем в ShopUser.referred_by_user_id для быстрых выборок без JOIN.
    invited = (
        await session.execute(
            select(ShopUser).where(ShopUser.id == referred_user_id)
        )
    ).scalar_one_or_none()
    if invited is not None and invited.referred_by_user_id is None:
        invited.referred_by_user_id = referrer_user_id

    await session.flush()
    logger.info(
        f"shop: referral {referrer_user_id} → {referred_user_id} attached"
    )
    return ref


async def apply_balance_change(
    session: AsyncSession,
    *,
    user_id: int,
    change_kopecks: int,
    reason: str,
    related_order_id: int | None = None,
    note: str | None = None,
) -> ShopUser:
    """
    Единая точка мутации баланса.

    Создаёт парную запись в ShopBalanceLedger И меняет ShopUser.balance_kopecks
    атомарно. Если change_kopecks==0 — no-op (защита от пустых записей).

    Защита от ухода в минус: списание (change<0), которое сделало бы
    balance отрицательным, поднимает ValueError. Это критично для
    предотвращения «бесплатных покупок» при race condition.
    """
    if change_kopecks == 0:
        return await _get_user_strict(session, user_id)

    user = await _get_user_strict(session, user_id)
    new_balance = user.balance_kopecks + change_kopecks
    if new_balance < 0:
        raise ValueError(
            f"insufficient balance: user={user_id} has "
            f"{user.balance_kopecks}, requested {change_kopecks}"
        )

    user.balance_kopecks = new_balance
    session.add(ShopBalanceLedger(
        user_id=user_id,
        change_kopecks=change_kopecks,
        reason=reason,
        related_order_id=related_order_id,
        note=note,
    ))
    await session.flush()
    logger.info(
        f"shop balance: user={user_id} "
        f"{'+%d' % change_kopecks if change_kopecks > 0 else change_kopecks} "
        f"(reason={reason}, new={new_balance})"
    )
    return user


async def _get_user_strict(session: AsyncSession, user_id: int) -> ShopUser:
    res = await session.execute(select(ShopUser).where(ShopUser.id == user_id))
    user = res.scalar_one_or_none()
    if user is None:
        raise ValueError(f"shop user not found: id={user_id}")
    return user


async def get_user_by_tg(
    session: AsyncSession, telegram_user_id: int
) -> Optional[ShopUser]:
    res = await session.execute(
        select(ShopUser).where(ShopUser.telegram_user_id == telegram_user_id)
    )
    return res.scalar_one_or_none()


def parse_referral_payload(payload: str | None) -> int | None:
    """
    Парсит `/start ref_123` или `/start 123` → 123 (id inviter'а).
    Возвращает None если payload пустой или не парсится.

    Telegram deep-link allows alphanumeric+underscore до 64 символов.
    Поддерживаем два формата:
        /start 123          (минималистично, для копирования)
        /start ref_123      (явный namespace, на случай добавления других deep-link'ов)
    """
    if not payload:
        return None
    payload = payload.strip()
    if payload.startswith("ref_"):
        payload = payload[4:]
    if not payload.isdigit():
        return None
    try:
        value = int(payload)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value
