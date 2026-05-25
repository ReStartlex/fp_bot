"""
Shop-репозиторий: атомарные операции, которые нельзя ломать.

Главное правило: любая мутация ShopUser.balance_kopecks ИДЁТ ЧЕРЕЗ
`apply_balance_change()`, которая создаёт парную запись в ShopBalanceLedger
в той же транзакции. Это даёт нам инвариант
`sum(ledger.change for user) == user.balance` навсегда.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from loguru import logger
from sqlalchemy import func, or_, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    ShopBalanceLedger,
    ShopCatalogCache,
    ShopReferral,
    ShopUser,
)


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


# ──────────────────────── Catalog cache ────────────────────────


@dataclass(frozen=True)
class CategoryGroup:
    """
    Группа NS-категорий, агрегированная по base_name (см. taxonomy.py).
    Используется на верхнем уровне UI каталога: «Apple Gift Card · 3 региона · от 396 ₽».
    """
    group_slug: str
    base_name: str
    # Сколько РАЗНЫХ NS-категорий внутри (региональные/платформенные варианты).
    variants_count: int
    # Сколько сервисов с in_stock>0 в этой группе суммарно.
    services_count: int
    cheapest_price_kopecks: int


@dataclass(frozen=True)
class CategoryInGroup:
    """Одна региональная/платформенная категория внутри группы (drill-down)."""
    category_id: int
    category_name: str
    services_count: int
    cheapest_price_kopecks: int


async def upsert_catalog_service(
    session: AsyncSession,
    *,
    ns_service_id: int,
    category_id: int | None,
    category_name: str | None,
    service_name: str,
    ns_price_usd: float,
    rub_price_kopecks: int,
    in_stock: int,
    fields_json: str | None,
    base_name: str | None = None,
    group_slug: str | None = None,
) -> ShopCatalogCache:
    """
    Идемпотентный upsert одного service'а в каталог. enabled НЕ меняется
    (если оператор выключил услугу через owner-бота — она остаётся off
    до явного включения).
    """
    res = await session.execute(
        select(ShopCatalogCache).where(
            ShopCatalogCache.ns_service_id == ns_service_id
        )
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = ShopCatalogCache(
            ns_service_id=ns_service_id,
            category_id=category_id,
            category_name=category_name,
            service_name=service_name,
            base_name=base_name,
            group_slug=group_slug,
            ns_price_usd=ns_price_usd,
            rub_price_kopecks=rub_price_kopecks,
            in_stock=in_stock,
            fields_json=fields_json,
            enabled=True,
        )
        session.add(row)
    else:
        row.category_id = category_id
        row.category_name = category_name
        row.service_name = service_name
        row.base_name = base_name
        row.group_slug = group_slug
        row.ns_price_usd = ns_price_usd
        row.rub_price_kopecks = rub_price_kopecks
        row.in_stock = in_stock
        row.fields_json = fields_json
        # fetched_at обновится автоматически через onupdate=func.now()
    await session.flush()
    return row


async def mark_services_unseen(
    session: AsyncSession,
    *,
    seen_service_ids: Iterable[int],
) -> int:
    """
    После полного fetch NS-каталога вызвать с set'ом service_id'ов,
    которые NS вернул. Все service_id'ы НЕ в этом set'е помечаются
    in_stock=0 — они «исчезли» из каталога NS. Не trash'им и не disable
    — оператор может явно их вернуть через owner-команды.

    Возвращает кол-во обновлённых записей.
    """
    seen = set(seen_service_ids)
    if not seen:
        # NS вернул пустой каталог — НЕ обнуляем cache (вероятно временный сбой NS).
        logger.warning(
            "shop catalog: mark_services_unseen called with empty seen set; "
            "skipping mass-zero to avoid wiping cache during NS hiccup"
        )
        return 0

    all_ids_res = await session.execute(
        select(ShopCatalogCache.ns_service_id, ShopCatalogCache.in_stock)
    )
    rows = list(all_ids_res.all())
    stale = [sid for sid, stock in rows if sid not in seen and stock > 0]
    if not stale:
        return 0
    await session.execute(
        sa_update(ShopCatalogCache)
        .where(ShopCatalogCache.ns_service_id.in_(stale))
        .values(in_stock=0)
    )
    await session.flush()
    logger.info(
        f"shop catalog: marked {len(stale)} services as out-of-stock "
        f"(no longer in NS catalog)"
    )
    return len(stale)


async def list_category_groups_for_ui(
    session: AsyncSession,
) -> list[CategoryGroup]:
    """
    Группировка по `base_name` (см. taxonomy.py). Все региональные/платформенные
    варианты одной игры сворачиваются в одну строку UI.

    Например: «Apple Gift Card | US», «Apple Gift Card | EU», «Apple Gift Card | UK»
    превратятся в одну группу «Apple Gift Card · 3 варианта · от 1 083 ₽».

    Категории без активных сервисов в выдачу не попадают.
    """
    stmt = (
        select(
            ShopCatalogCache.group_slug,
            func.min(ShopCatalogCache.base_name).label("base_name"),
            func.count(func.distinct(ShopCatalogCache.category_id)).label("variants"),
            func.count(ShopCatalogCache.ns_service_id).label("services"),
            func.min(ShopCatalogCache.rub_price_kopecks).label("cheapest"),
        )
        .where(
            ShopCatalogCache.enabled.is_(True),
            ShopCatalogCache.in_stock > 0,
            # Legacy fallback: записи, для которых catalog_sync ещё не
            # проставил group_slug, не попадают сюда; будут видны после
            # следующего sync'а (≤90 с). Это безопаснее, чем «пропихивать»
            # их в неструктурированный bucket.
            ShopCatalogCache.group_slug.is_not(None),
        )
        .group_by(ShopCatalogCache.group_slug)
        .order_by(func.lower(func.min(ShopCatalogCache.base_name)))
    )
    rows = (await session.execute(stmt)).all()
    return [
        CategoryGroup(
            group_slug=slug,
            base_name=bname or "Без категории",
            variants_count=variants,
            services_count=services,
            cheapest_price_kopecks=cheapest or 0,
        )
        for slug, bname, variants, services, cheapest in rows
    ]


# ════════════════════════════════════════════════════════════════════════
#                       Phase 1: balance & referral stats
# ════════════════════════════════════════════════════════════════════════
# Используются на страницах «💰 Баланс» и «👥 Рефералы» в shop-боте.
# Все запросы дешёвые (по индексам user_id) и переживут масштаб 100k юзеров.


@dataclass(frozen=True)
class BalanceStats:
    """Сводка для UI страницы баланса."""
    current_kopecks: int
    total_earned_kopecks: int   # сумма всех положительных движений ledger
    total_spent_kopecks: int    # сумма абсолютных значений отрицательных
    operations_count: int       # сколько строк в ledger вообще


@dataclass(frozen=True)
class LedgerRow:
    """Одна строка истории операций для UI."""
    created_at: datetime
    change_kopecks: int
    reason: str
    note: str | None
    related_order_id: int | None


@dataclass(frozen=True)
class ReferralStats:
    """Сводка для UI страницы рефералов."""
    invited_count: int          # сколько юзеров пришло по моей ссылке
    total_earned_kopecks: int   # сколько кэшбэка я уже получил
    active_referrals_count: int # из приглашённых: сколько было активно (last_seen ≤30д)


async def get_balance_stats(
    session: AsyncSession, *, user_id: int
) -> BalanceStats:
    """Агрегаты по ledger'у одного пользователя."""
    # Текущий баланс — из user (быстро).
    user = (
        await session.execute(
            select(ShopUser).where(ShopUser.id == user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        return BalanceStats(0, 0, 0, 0)

    # Суммируем earned/spent через CASE-выражение, одним запросом.
    from sqlalchemy import case
    earned_expr = func.coalesce(func.sum(
        case((ShopBalanceLedger.change_kopecks > 0,
              ShopBalanceLedger.change_kopecks), else_=0)
    ), 0)
    spent_expr = func.coalesce(func.sum(
        case((ShopBalanceLedger.change_kopecks < 0,
              -ShopBalanceLedger.change_kopecks), else_=0)
    ), 0)
    count_expr = func.count(ShopBalanceLedger.id)
    row = (
        await session.execute(
            select(earned_expr, spent_expr, count_expr)
            .where(ShopBalanceLedger.user_id == user_id)
        )
    ).one()
    earned, spent, cnt = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    return BalanceStats(
        current_kopecks=user.balance_kopecks,
        total_earned_kopecks=earned,
        total_spent_kopecks=spent,
        operations_count=cnt,
    )


async def list_balance_history(
    session: AsyncSession, *, user_id: int, limit: int = 10, offset: int = 0,
) -> tuple[list[LedgerRow], int]:
    """История операций по балансу, новые сверху. Возвращает (rows, total)."""
    base_q = (
        select(ShopBalanceLedger)
        .where(ShopBalanceLedger.user_id == user_id)
        .order_by(ShopBalanceLedger.created_at.desc(),
                  ShopBalanceLedger.id.desc())
    )
    total = int((await session.execute(
        select(func.count(ShopBalanceLedger.id))
        .where(ShopBalanceLedger.user_id == user_id)
    )).scalar() or 0)
    rows = list((
        await session.execute(base_q.limit(limit).offset(offset))
    ).scalars().all())
    return (
        [
            LedgerRow(
                created_at=r.created_at,
                change_kopecks=r.change_kopecks,
                reason=r.reason,
                note=r.note,
                related_order_id=r.related_order_id,
            )
            for r in rows
        ],
        total,
    )


async def get_referral_stats(
    session: AsyncSession, *, user_id: int,
) -> ReferralStats:
    """
    Сводка по рефералам пользователя:
      - invited_count: всего записей в shop_referrals, где referrer = user_id;
      - total_earned: сумма всех ledger.change_kopecks с reason='referral_cashback';
      - active_referrals: рефералы, активные за последние 30 дней
        (last_seen_at >= now - 30d).
    """
    invited = int((await session.execute(
        select(func.count(ShopReferral.id))
        .where(ShopReferral.referrer_user_id == user_id)
    )).scalar() or 0)

    earned = int((await session.execute(
        select(func.coalesce(func.sum(ShopBalanceLedger.change_kopecks), 0))
        .where(
            ShopBalanceLedger.user_id == user_id,
            ShopBalanceLedger.reason == "referral_cashback",
            ShopBalanceLedger.change_kopecks > 0,
        )
    )).scalar() or 0)

    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=30)
    active = int((await session.execute(
        select(func.count(ShopUser.id))
        .where(
            ShopUser.referred_by_user_id == user_id,
            ShopUser.last_seen_at >= cutoff,
        )
    )).scalar() or 0)

    return ReferralStats(
        invited_count=invited,
        total_earned_kopecks=earned,
        active_referrals_count=active,
    )


async def count_categories_in_group(
    session: AsyncSession, *, group_slug: str
) -> int:
    """
    Быстрый COUNT по distinct category_id в группе. Используется, чтобы
    решить: показывать кнопку «« Назад к группе» или «« К каталогу»
    в карточке услуги (для singleton-групп drill-down не имеет смысла —
    он просто возвращает на тот же экран).
    """
    stmt = (
        select(func.count(func.distinct(ShopCatalogCache.category_id)))
        .where(
            ShopCatalogCache.enabled.is_(True),
            ShopCatalogCache.in_stock > 0,
            ShopCatalogCache.group_slug == group_slug,
        )
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def list_categories_in_group(
    session: AsyncSession, *, group_slug: str
) -> list[CategoryInGroup]:
    """
    Drill-down: внутри группы (по group_slug) показать все её
    NS-категории — это региональные/платформенные варианты.

    Сортировка по category_name алфавитно (ASIA / CIS / EU / US / ...).
    """
    stmt = (
        select(
            ShopCatalogCache.category_id,
            ShopCatalogCache.category_name,
            func.count(ShopCatalogCache.ns_service_id).label("cnt"),
            func.min(ShopCatalogCache.rub_price_kopecks).label("cheapest"),
        )
        .where(
            ShopCatalogCache.enabled.is_(True),
            ShopCatalogCache.in_stock > 0,
            ShopCatalogCache.group_slug == group_slug,
        )
        .group_by(ShopCatalogCache.category_id, ShopCatalogCache.category_name)
        .order_by(ShopCatalogCache.category_name)
    )
    rows = (await session.execute(stmt)).all()
    return [
        CategoryInGroup(
            category_id=cid if cid is not None else 0,
            category_name=cname or "Без категории",
            services_count=cnt,
            cheapest_price_kopecks=cheapest or 0,
        )
        for cid, cname, cnt, cheapest in rows
    ]


async def search_services(
    session: AsyncSession,
    *,
    query: str,
    limit: int = 50,
) -> list[ShopCatalogCache]:
    """
    Поиск по shop-каталогу: LIKE по service_name и base_name (case-insensitive).
    Возвращает услуги отсортированные по цене.

    query: подстрока, минимум 2 символа после strip. Короче — пустой результат
    (избегаем выдачи «всех 5000 услуг» на пустую строку или 1 букву).
    """
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    pattern = f"%{q}%"
    stmt = (
        select(ShopCatalogCache)
        .where(
            ShopCatalogCache.enabled.is_(True),
            ShopCatalogCache.in_stock > 0,
            or_(
                func.lower(ShopCatalogCache.service_name).like(pattern),
                func.lower(ShopCatalogCache.base_name).like(pattern),
            ),
        )
        .order_by(
            ShopCatalogCache.rub_price_kopecks.asc(),
            ShopCatalogCache.service_name.asc(),
        )
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_services_in_category(
    session: AsyncSession,
    *,
    category_id: int,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ShopCatalogCache], int]:
    """
    Сервисы внутри одной категории, отсортированные по возрастанию цены.
    Возвращает (rows, total_count) для pagination.
    """
    where_clause = [
        ShopCatalogCache.enabled.is_(True),
        ShopCatalogCache.in_stock > 0,
    ]
    if category_id == 0:
        where_clause.append(ShopCatalogCache.category_id.is_(None))
    else:
        where_clause.append(ShopCatalogCache.category_id == category_id)

    count_stmt = (
        select(func.count(ShopCatalogCache.ns_service_id)).where(*where_clause)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(ShopCatalogCache)
        .where(*where_clause)
        .order_by(
            ShopCatalogCache.rub_price_kopecks.asc(),
            ShopCatalogCache.service_name.asc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def get_catalog_service(
    session: AsyncSession, ns_service_id: int
) -> Optional[ShopCatalogCache]:
    """Карточка одного сервиса (для checkout). None если нет или disabled."""
    res = await session.execute(
        select(ShopCatalogCache).where(
            ShopCatalogCache.ns_service_id == ns_service_id,
            ShopCatalogCache.enabled.is_(True),
        )
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
