"""
Публичный API витрины neurodrop.ru — `/api/public/*`.

В отличие от `shop_router.py` (Telegram Mini App), эти эндпоинты НЕ требуют
авторизации: их вызывает SSR-фронт сайта и поисковые роботы для индексации.
Только чтение каталога — никаких операций с балансом, заказами или оплатой
(они живут за авторизацией: Telegram Login на сайте / Mini App / бот).

Данные берутся из `shop_catalog_cache`, который фоновый воркер
`catalog_sync.sync_catalog_once` обновляет из NS каждые
`shop_catalog_refresh_seconds` (≈90с). Поэтому витрина всегда «тёплая»
и отвечает мгновенно, без обращения к NS в реальном времени.

Безопасность данных: наружу отдаём ТОЛЬКО витринные поля (рублёвая цена,
наличие, названия). Закупочная цена в USD (`ns_price_usd`) и схему
`fields_json` не раскрываем — это внутренняя экономика.

Кэширование: каждый ответ помечается `Cache-Control`, чтобы Next.js (ISR)
и любой CDN/обратный прокси могли кэшировать витрину и переживать всплески
трафика, не нагружая FastAPI.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Response

from src.db.models import ShopCatalogCache
from src.db.session import session_factory
from src.shop.repo import (
    get_catalog_service,
    get_catalog_totals,
    list_active_service_ids,
    list_categories_in_group,
    list_category_groups_for_ui,
    list_services_in_category,
    list_similar_services,
    search_services,
)

from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/public", tags=["public-site"])


# Витрина обновляется раз в ~90с — кэшируем на 60с и разрешаем отдавать
# «чуть устаревшее» ещё 120с, пока фон обновляет (stale-while-revalidate).
_CATALOG_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=120"
# Счётчики/sitemap меняются медленнее — кэшируем дольше.
_SLOW_CACHE_CONTROL = "public, max-age=300, stale-while-revalidate=600"


# ─── Schemas ───────────────────────────────────────────────────────


class PublicGroupOut(BaseModel):
    """Группа товаров на главной/в каталоге (бренд со всеми регионами)."""
    group_slug: str
    base_name: str
    variants_count: int
    services_count: int
    cheapest_price_kopecks: int


class PublicCategoryOut(BaseModel):
    """Региональный/платформенный вариант внутри группы."""
    category_id: int
    category_name: str
    services_count: int
    cheapest_price_kopecks: int


class PublicServiceOut(BaseModel):
    """Карточка товара для витрины (без раскрытия внутренней экономики)."""
    ns_service_id: int
    category_id: int | None
    category_name: str | None
    service_name: str
    base_name: str | None
    group_slug: str | None
    rub_price_kopecks: int
    in_stock: int


class PublicServiceCard(PublicServiceOut):
    """Расширенная карточка: + похожие товары для перелинковки и SEO."""
    similar: list[PublicServiceOut] = Field(default_factory=list)


class PublicServicesPage(BaseModel):
    items: list[PublicServiceOut]
    total: int
    page: int
    page_size: int


class PublicSearchResult(BaseModel):
    query: str
    items: list[PublicServiceOut]


class PublicStats(BaseModel):
    products_in_stock: int
    groups_count: int
    categories_count: int
    updated_at: str | None


class SitemapEntry(BaseModel):
    ns_service_id: int
    updated_at: str | None


class PublicSitemap(BaseModel):
    groups: list[str]
    services: list[SitemapEntry]


# ─── Helpers ───────────────────────────────────────────────────────


def _service_to_out(svc: ShopCatalogCache) -> PublicServiceOut:
    return PublicServiceOut(
        ns_service_id=svc.ns_service_id,
        category_id=svc.category_id,
        category_name=svc.category_name,
        service_name=svc.service_name,
        base_name=svc.base_name,
        group_slug=svc.group_slug,
        rub_price_kopecks=svc.rub_price_kopecks,
        in_stock=svc.in_stock,
    )


# ─── Endpoints ─────────────────────────────────────────────────────


@router.get("/catalog/groups", response_model=list[PublicGroupOut])
async def public_groups(response: Response):
    """Главный экран каталога: бренды/группы с числом вариантов и «от N ₽»."""
    response.headers["Cache-Control"] = _CATALOG_CACHE_CONTROL
    async with session_factory()() as session:
        groups = await list_category_groups_for_ui(session)
    return [
        PublicGroupOut(
            group_slug=g.group_slug,
            base_name=g.base_name,
            variants_count=g.variants_count,
            services_count=g.services_count,
            cheapest_price_kopecks=g.cheapest_price_kopecks,
        )
        for g in groups
    ]


@router.get("/catalog/groups/{slug}", response_model=list[PublicCategoryOut])
async def public_group_variants(slug: str, response: Response):
    """Варианты (регионы/платформы) внутри группы."""
    response.headers["Cache-Control"] = _CATALOG_CACHE_CONTROL
    async with session_factory()() as session:
        variants = await list_categories_in_group(session, group_slug=slug)
    return [
        PublicCategoryOut(
            category_id=v.category_id,
            category_name=v.category_name,
            services_count=v.services_count,
            cheapest_price_kopecks=v.cheapest_price_kopecks,
        )
        for v in variants
    ]


@router.get("/catalog/categories/{category_id}", response_model=PublicServicesPage)
async def public_category_services(
    category_id: int,
    response: Response,
    page: int = Query(0, ge=0),
    page_size: int = Query(40, ge=1, le=100),
    sort: str = Query("price_asc"),
):
    """Список номиналов внутри одной NS-категории (с пагинацией и сортировкой)."""
    response.headers["Cache-Control"] = _CATALOG_CACHE_CONTROL
    offset = page * page_size
    sort = sort if sort in ("price_asc", "price_desc") else "price_asc"
    async with session_factory()() as session:
        rows, total = await list_services_in_category(
            session, category_id=category_id, limit=page_size, offset=offset,
            sort=sort,
        )
    return PublicServicesPage(
        items=[_service_to_out(s) for s in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/catalog/services/{ns_service_id}", response_model=PublicServiceCard)
async def public_service_card(ns_service_id: int, response: Response):
    """Карточка одного товара + похожие (другие номиналы/регионы того же бренда)."""
    async with session_factory()() as session:
        svc = await get_catalog_service(session, ns_service_id=ns_service_id)
        if svc is None or int(svc.in_stock or 0) <= 0:
            # 404 как для отсутствующего, так и для распроданного — фронт
            # покажет «нет в наличии», робот не проиндексирует мёртвую цену.
            response.status_code = 404
            return PublicServiceCard(
                ns_service_id=ns_service_id, category_id=None,
                category_name=None, service_name="", base_name=None,
                group_slug=None, rub_price_kopecks=0, in_stock=0,
            )
        similar = await list_similar_services(
            session, ns_service_id=ns_service_id, limit=6,
        )
    response.headers["Cache-Control"] = _CATALOG_CACHE_CONTROL
    base = _service_to_out(svc)
    return PublicServiceCard(
        **base.model_dump(),
        similar=[_service_to_out(s) for s in similar],
    )


@router.get("/search", response_model=PublicSearchResult)
async def public_search(
    response: Response,
    q: str = Query("", description="Поисковая фраза (мин. 2 символа)"),
    limit: int = Query(50, ge=1, le=100),
):
    """Поиск по витрине: LIKE по названию и бренду."""
    response.headers["Cache-Control"] = _CATALOG_CACHE_CONTROL
    async with session_factory()() as session:
        results = await search_services(session, query=q, limit=limit)
    return PublicSearchResult(
        query=q,
        items=[_service_to_out(s) for s in results],
    )


@router.get("/stats", response_model=PublicStats)
async def public_stats(response: Response):
    """Счётчики для главной: «N товаров в наличии», «M брендов»."""
    response.headers["Cache-Control"] = _SLOW_CACHE_CONTROL
    async with session_factory()() as session:
        totals = await get_catalog_totals(session)
    return PublicStats(
        products_in_stock=totals.products_in_stock,
        groups_count=totals.groups_count,
        categories_count=totals.categories_count,
        updated_at=totals.updated_at.isoformat() if totals.updated_at else None,
    )


@router.get("/sitemap", response_model=PublicSitemap)
async def public_sitemap(response: Response):
    """Данные для генерации sitemap.xml на стороне сайта."""
    response.headers["Cache-Control"] = _SLOW_CACHE_CONTROL
    async with session_factory()() as session:
        groups = await list_category_groups_for_ui(session)
        services = await list_active_service_ids(session)
    return PublicSitemap(
        groups=[g.group_slug for g in groups],
        services=[
            SitemapEntry(
                ns_service_id=sid,
                updated_at=fetched.isoformat() if fetched else None,
            )
            for sid, fetched in services
        ],
    )
