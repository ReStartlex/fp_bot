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
    ShopOrder,
    ShopPayment,
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


async def list_similar_services(
    session: AsyncSession,
    *,
    ns_service_id: int,
    limit: int = 5,
) -> list[ShopCatalogCache]:
    """
    Возвращает «похожие» услуги — другие сервисы того же бренда (base_name)
    из других category_id, чтобы покупатель видел альтернативные регионы/
    номиналы прямо в карточке товара.

    Алгоритм:
      1. Найти services_id → base_name (исходный сервис);
      2. Выбрать enabled+in_stock>0 услуги с тем же base_name, ИСКЛЮЧАЯ
         сам исходный;
      3. Отсортировать по цене (растущая) — обычно дешёвые номиналы более
         популярны;
      4. Limit (default 5, отдадим в UI до 3 — но запас на случай дубликатов).

    Возвращает [] если:
      * исходный сервис не найден / disabled;
      * у исходного нет base_name (тогда «похожих» определить нельзя);
      * других услуг этого бренда нет.

    NOTE: не используем `group_slug` напрямую, потому что в БД он может
    быть NULL для старых записей (миграция backfill'ила, но всё же).
    base_name гарантированно есть после catalog_sync.
    """
    if limit <= 0:
        return []
    origin = await get_catalog_service(session, ns_service_id)
    if origin is None or not origin.base_name:
        return []
    stmt = (
        select(ShopCatalogCache)
        .where(
            ShopCatalogCache.enabled.is_(True),
            ShopCatalogCache.in_stock > 0,
            ShopCatalogCache.base_name == origin.base_name,
            ShopCatalogCache.ns_service_id != ns_service_id,
        )
        .order_by(ShopCatalogCache.rub_price_kopecks.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# ════════════════════════════════════════════════════════════════════════
#                    Phase 1: payments (CryptoBot и далее)
# ════════════════════════════════════════════════════════════════════════
# ShopPayment — независимый объект, отслеживающий жизненный цикл одной
# попытки оплаты. Связан с user_id (для top-up) или order_id (для checkout
# заказа, появится в следующем спринте).
#
# Контракт идемпотентности:
#   - (provider, provider_invoice_id) — UNIQUE в БД (см. модель);
#   - apply_paid_invoice() безопасен для повторного вызова — повторное
#     событие "paid" по тому же invoice = no-op (без двойного начисления).
#   - Любая мутация баланса проходит через apply_balance_change(), которая
#     сохраняет invariant `sum(ledger.change for user) == user.balance`.


async def create_topup_payment(
    session: AsyncSession,
    *,
    user_id: int,
    provider: str,
    provider_invoice_id: str,
    amount_kopecks: int,
    currency: str = "RUB",
    raw_payload_json: str | None = None,
    notify_telegram_id: int | None = None,
) -> ShopPayment:
    """
    Создаёт запись ShopPayment в статусе 'pending' для пополнения баланса.

    Если payment с такой парой (provider, provider_invoice_id) уже есть —
    возвращает существующую (idempotent insert). Это нужно, чтобы при
    повторной попытке (юзер дабл-кликнул кнопку) не создавать дубликаты.
    """
    res = await session.execute(
        select(ShopPayment).where(
            ShopPayment.provider == provider,
            ShopPayment.provider_invoice_id == provider_invoice_id,
        )
    )
    existing = res.scalar_one_or_none()
    if existing is not None:
        return existing
    p = ShopPayment(
        order_id=None,
        provider=provider,
        provider_invoice_id=provider_invoice_id,
        amount_kopecks=amount_kopecks,
        currency=currency,
        status="pending",
        raw_payload_json=raw_payload_json,
    )
    # user_id ShopPayment в модели нет напрямую (там order_id для checkout),
    # поэтому для top-up без заказа храним user_id в raw_payload_json под
    # ключом topup_user_id. Это позволит apply_paid_invoice найти юзера.
    # Альтернатива — добавить колонку user_id; ALTER потом, в Sprint 3.5,
    # когда схема стабилизируется.
    import json as _json
    payload_dict: dict = _json.loads(raw_payload_json or "{}") if raw_payload_json else {}
    payload_dict["topup_user_id"] = user_id
    if notify_telegram_id is not None:
        payload_dict["notify_telegram_id"] = notify_telegram_id
    p.raw_payload_json = _json.dumps(payload_dict, ensure_ascii=False)
    session.add(p)
    await session.flush()
    return p


async def get_payment(
    session: AsyncSession, *, provider: str, provider_invoice_id: str,
) -> ShopPayment | None:
    res = await session.execute(
        select(ShopPayment).where(
            ShopPayment.provider == provider,
            ShopPayment.provider_invoice_id == provider_invoice_id,
        )
    )
    return res.scalar_one_or_none()


async def list_pending_payments_for_user(
    session: AsyncSession, *, user_id: int, provider: str | None = None,
) -> list[ShopPayment]:
    """
    Pending-платежи юзера (для UI «🔄 Проверить статус»). Берём из
    raw_payload_json по ключу topup_user_id.

    Не fancy — в SQLite нет JSON-индексов, делаем LIKE по строке.
    На горизонте 1000 платежей юзеру это всё ещё <10мс, не проблема.
    """
    pattern = f'%"topup_user_id": {user_id}%'
    stmt = (
        select(ShopPayment)
        .where(
            ShopPayment.status == "pending",
            ShopPayment.raw_payload_json.like(pattern),
        )
        .order_by(ShopPayment.created_at.desc())
        .limit(20)
    )
    if provider is not None:
        stmt = stmt.where(ShopPayment.provider == provider)
    return list((await session.execute(stmt)).scalars().all())


async def apply_paid_invoice(
    session: AsyncSession,
    *,
    provider: str,
    provider_invoice_id: str,
    paid_amount_kopecks: int,
    paid_at: datetime | None = None,
    raw_payload_json: str | None = None,
) -> tuple[ShopPayment, bool]:
    """
    Идемпотентно отметить платёж как оплаченный и начислить юзеру баланс.

    Возвращает (payment, was_just_applied):
      - was_just_applied=True  — это первое 'paid'-событие, баланс начислен;
      - was_just_applied=False — payment уже был 'paid' / 'failed', no-op.

    Безопасно вызывать многократно: повторное событие webhook / polling
    об одном и том же invoice'е не приведёт к двойному начислению.

    Платёж должен существовать в БД (создан через create_topup_payment).
    Если не найден — возвращает (None, False).
    """
    res = await session.execute(
        select(ShopPayment).where(
            ShopPayment.provider == provider,
            ShopPayment.provider_invoice_id == provider_invoice_id,
        )
    )
    payment = res.scalar_one_or_none()
    if payment is None:
        logger.warning(
            f"apply_paid_invoice: unknown payment {provider}:{provider_invoice_id}"
        )
        return None, False  # type: ignore[return-value]
    if payment.status == "paid":
        return payment, False  # already applied
    if payment.status not in ("pending",):
        # 'failed' или иной финальный статус — не пере-открываем
        return payment, False

    # Достаём user_id из raw_payload_json (для top-up'ов).
    import json as _json
    user_id: int | None = None
    if payment.raw_payload_json:
        try:
            user_id = int(_json.loads(payment.raw_payload_json).get("topup_user_id"))
        except (ValueError, TypeError, KeyError):
            user_id = None

    payment.status = "paid"
    payment.paid_at = paid_at or datetime.utcnow()
    if raw_payload_json is not None:
        # Дополнить raw — оставляем topup_user_id, добавляем paid-данные
        try:
            old = _json.loads(payment.raw_payload_json or "{}")
        except ValueError:
            old = {}
        try:
            new = _json.loads(raw_payload_json)
        except ValueError:
            new = {"raw": raw_payload_json}
        old.update({"paid_payload": new})
        payment.raw_payload_json = _json.dumps(old, ensure_ascii=False)

    if user_id is not None and paid_amount_kopecks > 0:
        await apply_balance_change(
            session,
            user_id=user_id,
            change_kopecks=paid_amount_kopecks,
            reason="manual_topup",
            note=f"{provider}:{provider_invoice_id}",
        )
        logger.info(
            f"apply_paid_invoice: +{paid_amount_kopecks} kop user={user_id} "
            f"({provider}:{provider_invoice_id})"
        )
    else:
        logger.warning(
            f"apply_paid_invoice: payment {payment.id} paid but no user_id "
            f"or zero amount; balance not credited"
        )
    await session.flush()
    return payment, True


async def mark_payment_failed(
    session: AsyncSession,
    *,
    provider: str,
    provider_invoice_id: str,
    reason: str,
) -> ShopPayment | None:
    """Финальный 'failed' статус (истёкший invoice). Идемпотентен."""
    res = await session.execute(
        select(ShopPayment).where(
            ShopPayment.provider == provider,
            ShopPayment.provider_invoice_id == provider_invoice_id,
        )
    )
    payment = res.scalar_one_or_none()
    if payment is None or payment.status != "pending":
        return payment
    payment.status = "failed"
    payment.error = reason
    await session.flush()
    return payment


# ════════════════════════════════════════════════════════════════════════
#               Sprint 5: shop checkout — заказы и cashback
# ════════════════════════════════════════════════════════════════════════
# Жизненный цикл ShopOrder:
#   draft → paid → delivering → delivered
#                          │
#                          └─→ failed → refunded
#
# Идемпотентность критична:
#   * Заказ paid → balance уже списан (apply_balance_change с reason="order_payment");
#   * Заказ delivered → cashback начислен инвайтеру ИЛИ зафиксирована попытка;
#   * Заказ failed → balance возвращён покупателю (reason="refund") ОДИН раз.
# Любая повторная попытка тех же шагов = no-op.
#
# Гарантии:
#   1. credit_referral_cashback — проверяет наличие ledger-записи
#      (user_id, related_order_id, reason="referral_cashback") и не дублирует.
#   2. refund_failed_order — проверяет наличие ledger-записи
#      (user_id, related_order_id, reason="refund") и не дублирует.
#   3. mark_order_* — переходы по статусам однонаправленные; обратное
#      движение требует явных функций (например refund после delivered).


# Финальные статусы — заказ больше не движется по обычному пайплайну.
# Любые операции (cashback, refund) ДОЛЖНЫ быть выполнены ровно один раз.
SHOP_ORDER_STATUS_DRAFT = "draft"
SHOP_ORDER_STATUS_PAID = "paid"
SHOP_ORDER_STATUS_DELIVERING = "delivering"
SHOP_ORDER_STATUS_DELIVERED = "delivered"
SHOP_ORDER_STATUS_FAILED = "failed"
SHOP_ORDER_STATUS_REFUNDED = "refunded"

# Reason-константы для ledger — используются как идентификаторы операций
# (см. UNIQUE-логику в credit_referral_cashback / refund_failed_order).
LEDGER_REASON_ORDER_PAYMENT = "order_payment"
LEDGER_REASON_REFERRAL_CASHBACK = "referral_cashback"
LEDGER_REASON_REFUND = "refund"


async def _ledger_has_entry(
    session: AsyncSession,
    *,
    user_id: int,
    related_order_id: int,
    reason: str,
) -> bool:
    """
    True, если в ledger есть хотя бы одна запись по этому (user, order, reason).

    Используется для guard'ов идемпотентности: «начислять cashback ТОЛЬКО
    если ещё не начислили». Поскольку ledger — append-only, наличие записи
    означает «операция уже выполнена».
    """
    res = await session.execute(
        select(ShopBalanceLedger.id)
        .where(
            ShopBalanceLedger.user_id == user_id,
            ShopBalanceLedger.related_order_id == related_order_id,
            ShopBalanceLedger.reason == reason,
        )
        .limit(1)
    )
    return res.scalar_one_or_none() is not None


async def create_shop_order(
    session: AsyncSession,
    *,
    user_id: int,
    ns_service_id: int,
    ns_service_name: str,
    fields_json: str = "[]",
    quantity: int = 1,
    total_rub_kopecks: int,
    ns_price_usd: float | None = None,
    fx_rate_at_sale: float | None = None,
    markup_percent_at_sale: float | None = None,
    payment_method: str = "balance_only",
) -> ShopOrder:
    """
    Создаёт ShopOrder в статусе DRAFT. Деньги ещё не списаны.

    `payment_method`:
      * "balance_only" — оплата полностью с внутреннего баланса (Sprint 5);
      * "cryptobot" / "stars" — частичная/полная оплата извне (Sprint 5.2+).

    Возвращает ShopOrder с заполненным id (после flush).
    """
    order = ShopOrder(
        user_id=user_id,
        ns_service_id=ns_service_id,
        ns_service_name=ns_service_name,
        fields_json=fields_json,
        quantity=quantity,
        total_rub_kopecks=total_rub_kopecks,
        ns_price_usd=ns_price_usd,
        fx_rate_at_sale=fx_rate_at_sale,
        markup_percent_at_sale=markup_percent_at_sale,
        payment_method=payment_method,
        status=SHOP_ORDER_STATUS_DRAFT,
    )
    session.add(order)
    await session.flush()
    return order


async def get_shop_order(
    session: AsyncSession, order_id: int
) -> ShopOrder | None:
    """Read-only выборка одного заказа по id."""
    res = await session.execute(
        select(ShopOrder).where(ShopOrder.id == order_id)
    )
    return res.scalar_one_or_none()


async def mark_order_paid(
    session: AsyncSession,
    *,
    order_id: int,
    balance_used_kopecks: int,
    external_paid_kopecks: int = 0,
) -> ShopOrder:
    """
    DRAFT → PAID. Записывает фактически использованную часть с баланса
    и с внешней оплаты. paid_at = now.

    NB: предполагается, что balance уже дебитован вызывающим (через
    apply_balance_change). Эта функция не трогает ledger.
    """
    order = await get_shop_order(session, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    order.status = SHOP_ORDER_STATUS_PAID
    order.balance_used_kopecks = balance_used_kopecks
    order.external_paid_kopecks = external_paid_kopecks
    order.paid_at = datetime.utcnow()
    await session.flush()
    return order


async def mark_order_delivering(
    session: AsyncSession,
    *,
    order_id: int,
    ns_custom_id: str,
    ns_order_id: str | None = None,
) -> ShopOrder:
    """PAID → DELIVERING. Перешли в фазу NS-выдачи."""
    order = await get_shop_order(session, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    order.status = SHOP_ORDER_STATUS_DELIVERING
    order.ns_custom_id = ns_custom_id
    if ns_order_id is not None:
        order.ns_order_id = ns_order_id
    await session.flush()
    return order


async def mark_order_delivered(
    session: AsyncSession,
    *,
    order_id: int,
    pins_json: str,
) -> ShopOrder:
    """DELIVERING → DELIVERED. Сохраняем pins/contents, отмечаем delivered_at."""
    order = await get_shop_order(session, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    order.status = SHOP_ORDER_STATUS_DELIVERED
    order.pins_json = pins_json
    order.delivered_at = datetime.utcnow()
    await session.flush()
    return order


async def mark_order_failed(
    session: AsyncSession,
    *,
    order_id: int,
    error: str,
) -> ShopOrder:
    """
    Любой статус → FAILED. Записывает текст ошибки.

    NB: вызов НЕ возвращает деньги — это отдельная операция
    refund_failed_order, чтобы было видно «упало → ещё не возвращено».
    """
    order = await get_shop_order(session, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    order.status = SHOP_ORDER_STATUS_FAILED
    order.error = (error or "")[:1000]
    await session.flush()
    return order


async def refund_failed_order(
    session: AsyncSession,
    *,
    order_id: int,
) -> ShopUser | None:
    """
    FAILED → REFUNDED. Возвращает balance_used_kopecks покупателю.

    Идемпотентно: если refund уже сделан (есть ledger entry с
    reason="refund" и related_order_id=order_id), возвращает None без
    изменения баланса.

    Это нужно для случая когда delivery worker дважды попытался обработать
    тот же failed-заказ — баланс вернётся ровно один раз.
    """
    order = await get_shop_order(session, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    if order.status not in (SHOP_ORDER_STATUS_FAILED, SHOP_ORDER_STATUS_DELIVERING):
        # REFUNDED / DELIVERED — ничего не делаем; иначе попасть сюда
        # из обычного flow невозможно.
        return None
    if order.balance_used_kopecks <= 0:
        # Заказ оплачен полностью извне (e.g. CryptoBot) — refund'ы
        # внешних провайдеров обрабатываются по их API, не нашему ledger.
        order.status = SHOP_ORDER_STATUS_REFUNDED
        await session.flush()
        return None

    # Guard идемпотентности
    already_refunded = await _ledger_has_entry(
        session,
        user_id=order.user_id,
        related_order_id=order_id,
        reason=LEDGER_REASON_REFUND,
    )
    if already_refunded:
        # Помечаем статус как REFUNDED (если ещё нет) — не двигаем баланс.
        order.status = SHOP_ORDER_STATUS_REFUNDED
        await session.flush()
        return None

    user = await apply_balance_change(
        session,
        user_id=order.user_id,
        change_kopecks=order.balance_used_kopecks,
        reason=LEDGER_REASON_REFUND,
        related_order_id=order_id,
        note=f"Refund for failed order #{order_id}",
    )
    order.status = SHOP_ORDER_STATUS_REFUNDED
    await session.flush()
    return user


async def credit_referral_cashback(
    session: AsyncSession,
    *,
    order_id: int,
    cashback_percent: float,
) -> int:
    """
    Начисляет cashback инвайтеру покупателя за заказ ORDER_ID.

    Алгоритм:
      1. Берём заказ — должен быть DELIVERED;
      2. Находим покупателя → его referred_by_user_id (= inviter);
      3. Если нет inviter'а — пропускаем (return 0);
      4. Считаем cashback = floor(order.total * cashback_percent / 100);
      5. Если cashback ≤ 0 — пропускаем (return 0);
      6. **Idempotency guard**: ищем существующую ledger запись с
         (inviter_id, related_order_id=order_id, reason="referral_cashback").
         Если есть — пропускаем (return 0 — уже начислено).
      7. apply_balance_change(+cashback, reason="referral_cashback",
         related_order_id=order_id).

    Возвращает реально начисленную сумму (kopecks). 0 = ничего не
    сделали (нет inviter'а, либо уже начисляли, либо cashback < 1 коп).

    Безопасно вызывать многократно: повторные вызовы возвращают 0.
    """
    if cashback_percent <= 0:
        return 0
    order = await get_shop_order(session, order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    if order.status != SHOP_ORDER_STATUS_DELIVERED:
        # Защита от случая «cashback начислили до того как заказ был
        # delivered». Это бы дало юзеру деньги до того как поставщик
        # подтвердил выдачу.
        return 0

    buyer = await _get_user_strict(session, order.user_id)
    inviter_id = buyer.referred_by_user_id
    if inviter_id is None or inviter_id == buyer.id:
        return 0

    # Cashback в копейках. floor — чтобы не давать дробных, ledger
    # хранит целые копейки.
    cashback_kopecks = int(order.total_rub_kopecks * cashback_percent / 100.0)
    if cashback_kopecks <= 0:
        return 0

    # Idempotency
    already_credited = await _ledger_has_entry(
        session,
        user_id=inviter_id,
        related_order_id=order_id,
        reason=LEDGER_REASON_REFERRAL_CASHBACK,
    )
    if already_credited:
        return 0

    # Проверяем что inviter существует (могли удалить или это битый id)
    inviter_row = await session.execute(
        select(ShopUser).where(ShopUser.id == inviter_id)
    )
    if inviter_row.scalar_one_or_none() is None:
        return 0

    await apply_balance_change(
        session,
        user_id=inviter_id,
        change_kopecks=cashback_kopecks,
        reason=LEDGER_REASON_REFERRAL_CASHBACK,
        related_order_id=order_id,
        note=f"Cashback {cashback_percent}% for order #{order_id}",
    )
    return cashback_kopecks


async def list_user_orders(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[ShopOrder], int]:
    """
    Список заказов покупателя для UI «📦 Мои заказы». Newest first.

    Возвращает (rows, total_count) — total нужен для пагинации.
    """
    where = ShopOrder.user_id == user_id
    total = (await session.execute(
        select(func.count(ShopOrder.id)).where(where)
    )).scalar_one()
    rows = (await session.execute(
        select(ShopOrder)
        .where(where)
        .order_by(ShopOrder.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()
    return list(rows), total


async def list_orders_awaiting_delivery(
    session: AsyncSession,
    *,
    limit: int = 10,
) -> list[ShopOrder]:
    """
    PAID + DELIVERING заказы — кандидаты для delivery worker'а.

    Сортировка: по id ASC (FIFO — раньше оплаченные сначала). Limit
    защищает от «возьмём все 1000 заказов разом и упадём».
    """
    res = await session.execute(
        select(ShopOrder)
        .where(ShopOrder.status.in_([
            SHOP_ORDER_STATUS_PAID,
            SHOP_ORDER_STATUS_DELIVERING,
        ]))
        .order_by(ShopOrder.id.asc())
        .limit(limit)
    )
    return list(res.scalars().all())


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
