"""
Sprint 6 — FastAPI router /api/shop/* для Telegram Mini App.

Все endpoints авторизуются через Telegram WebApp initData (заголовок
`X-Telegram-Init-Data`). Никакого Bearer-токена — initData самодостаточен
(содержит user_id и HMAC от bot_token).

Endpoints (MVP):
  POST /api/shop/init              — авторизация + получение user info
  GET  /api/shop/me                — баланс + статистика
  GET  /api/shop/catalog/groups    — главный экран каталога
  GET  /api/shop/catalog/groups/{slug}    — варианты группы
  GET  /api/shop/catalog/categories/{id}  — список услуг
  GET  /api/shop/catalog/services/{id}    — карточка
  POST /api/shop/checkout          — атомарный buy (создаёт ShopOrder paid)
  GET  /api/shop/orders            — история заказов
  GET  /api/shop/orders/{id}       — карточка одного заказа

Что Mini App НЕ умеет (специально, MVP):
  * Поиск (через бот: 🔍 Поиск)
  * Top-up (через бот: 💰 Баланс → 🪙 CryptoBot)
  * Реферальная ссылка (через бот: 👥 Рефералы)

Это сделано чтобы Mini App был лёгким и быстрым; продвинутые операции
делаются в боте, где UX уже отполирован.

Авторизация: dependency `current_webapp_user` валидирует initData и
возвращает соответствующий ShopUser (создаёт если новый — точно как
`/start` в боте делает).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.webapp_auth import (
    WebAppAuthError,
    WebAppInitData,
    verify_init_data,
)
from src.config import Settings, get_settings
from src.config_runtime import get_shop_referral_percent
from src.db.models import ShopUser
from src.db.session import session_factory
from src.shop.checkout import CheckoutOutcome, attempt_checkout_via_balance
from src.shop.repo import (
    apply_balance_change as _unused_apply,  # noqa: F401 (для self-doc)
    get_balance_stats,
    get_catalog_service,
    get_or_create_user,
    get_referral_stats,
    get_shop_order,
    list_categories_in_group,
    list_category_groups_for_ui,
    list_services_in_category,
    list_user_orders,
)


router = APIRouter(prefix="/api/shop", tags=["shop-miniapp"])


# ─── Auth dependency ──────────────────────────────────────────────


async def current_webapp_user(
    x_telegram_init_data: str = Header(
        ...,
        alias="X-Telegram-Init-Data",
        description="Telegram WebApp initData (window.Telegram.WebApp.initData)",
    ),
    settings: Settings = Depends(get_settings),
) -> tuple[WebAppInitData, ShopUser]:
    """
    Валидирует initData, ленится-создаёт ShopUser, возвращает пару (init, user).

    Шаги:
      1. Прочитать токен shop-бота из settings (не bridge bot!);
      2. verify_init_data → проверка hash + auth_date;
      3. get_or_create_user(telegram_user_id=init.user.id);
      4. Если user.blocked — 403.

    Бросает HTTPException(401) при невалидном initData, чтобы фронт
    мог отрендерить «не доступа».
    """
    if not settings.shop_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shop is disabled",
        )
    token_secret = settings.shop_telegram_bot_token
    if token_secret is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shop bot token not configured",
        )
    bot_token = (
        token_secret.get_secret_value()
        if hasattr(token_secret, "get_secret_value")
        else str(token_secret)
    )
    try:
        init = verify_init_data(x_telegram_init_data, bot_token=bot_token)
    except WebAppAuthError as exc:
        logger.debug(f"webapp auth rejected: {exc}")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"init_data invalid: {exc}",
        )

    async with session_factory()() as session:
        user, _is_new = await get_or_create_user(
            session,
            telegram_user_id=init.user.id,
            telegram_username=init.user.username or None,
            first_name=init.user.first_name or None,
            language_code=init.user.language_code or None,
        )
        await session.commit()

    if user.blocked:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="user is blocked",
        )
    return init, user


# ─── Schemas (Pydantic) ───────────────────────────────────────────


class MeResponse(BaseModel):
    user_id: int
    telegram_user_id: int
    username: str | None
    first_name: str | None
    balance_kopecks: int
    # Статистика
    total_earned_kopecks: int
    total_spent_kopecks: int
    operations_count: int
    # Рефералы
    invited_count: int
    active_referrals_count: int
    earned_via_referrals_kopecks: int
    referral_percent: float


class GroupOut(BaseModel):
    group_slug: str
    base_name: str
    variants_count: int
    cheapest_price_kopecks: int


class CategoryOut(BaseModel):
    category_id: int
    category_name: str
    cheapest_price_kopecks: int


class ServiceOut(BaseModel):
    ns_service_id: int
    category_id: int | None
    category_name: str | None
    service_name: str
    base_name: str | None
    group_slug: str | None
    rub_price_kopecks: int
    in_stock: int


class CheckoutRequest(BaseModel):
    ns_service_id: int = Field(..., ge=1)


class CheckoutResponseOut(BaseModel):
    outcome: str
    order_id: int | None = None
    new_balance_kopecks: int | None = None
    # Только при insufficient
    need_kopecks: int | None = None
    have_kopecks: int | None = None
    deficit_kopecks: int | None = None


class OrderOut(BaseModel):
    id: int
    ns_service_id: int
    ns_service_name: str
    total_rub_kopecks: int
    status: str
    created_at: str
    delivered_at: str | None
    pins: list | None = None
    error: str | None = None


class OrderListResponse(BaseModel):
    orders: list[OrderOut]
    total: int
    page: int
    page_size: int


# ─── Endpoints ────────────────────────────────────────────────────


@router.post("/init", response_model=MeResponse)
async def init_endpoint(
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
    settings: Settings = Depends(get_settings),
):
    """
    Authentication / handshake.

    Mini App вызывает этот endpoint при старте; в ответ получает user info,
    баланс, статистику. Дальше можно дёргать `/me` для refresh'а.
    """
    return await _build_me_response(auth, settings)


@router.get("/me", response_model=MeResponse)
async def me_endpoint(
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
    settings: Settings = Depends(get_settings),
):
    return await _build_me_response(auth, settings)


async def _build_me_response(
    auth: tuple[WebAppInitData, ShopUser],
    settings: Settings,
) -> MeResponse:
    init, user = auth
    async with session_factory()() as session:
        bal = await get_balance_stats(session, user_id=user.id)
        ref = await get_referral_stats(session, user_id=user.id)
    referral_percent = await get_shop_referral_percent(settings)
    return MeResponse(
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        username=user.telegram_username,
        first_name=user.first_name,
        balance_kopecks=user.balance_kopecks,
        total_earned_kopecks=bal.total_earned_kopecks,
        total_spent_kopecks=bal.total_spent_kopecks,
        operations_count=bal.operations_count,
        invited_count=ref.invited_count,
        active_referrals_count=ref.active_referrals_count,
        earned_via_referrals_kopecks=ref.total_earned_kopecks,
        referral_percent=referral_percent,
    )


@router.get("/catalog/groups", response_model=list[GroupOut])
async def catalog_groups_endpoint(
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
):
    async with session_factory()() as session:
        groups = await list_category_groups_for_ui(session)
    return [
        GroupOut(
            group_slug=g.group_slug,
            base_name=g.base_name,
            variants_count=g.variants_count,
            cheapest_price_kopecks=g.cheapest_price_kopecks,
        )
        for g in groups
    ]


@router.get("/catalog/groups/{slug}", response_model=list[CategoryOut])
async def catalog_group_variants_endpoint(
    slug: str,
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
):
    async with session_factory()() as session:
        variants = await list_categories_in_group(session, group_slug=slug)
    return [
        CategoryOut(
            category_id=v.category_id,
            category_name=v.category_name,
            cheapest_price_kopecks=v.cheapest_price_kopecks,
        )
        for v in variants
    ]


@router.get("/catalog/categories/{category_id}", response_model=list[ServiceOut])
async def catalog_category_services_endpoint(
    category_id: int,
    limit: int = 100,
    offset: int = 0,
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
):
    async with session_factory()() as session:
        services, _total = await list_services_in_category(
            session, category_id=category_id, limit=limit, offset=offset,
        )
    return [_service_to_out(s) for s in services]


@router.get("/catalog/services/{ns_service_id}", response_model=ServiceOut)
async def catalog_service_endpoint(
    ns_service_id: int,
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
):
    async with session_factory()() as session:
        svc = await get_catalog_service(session, ns_service_id=ns_service_id)
    if svc is None:
        raise HTTPException(404, "service not found")
    return _service_to_out(svc)


def _service_to_out(svc) -> ServiceOut:
    return ServiceOut(
        ns_service_id=svc.ns_service_id,
        category_id=svc.category_id,
        category_name=svc.category_name,
        service_name=svc.service_name,
        base_name=svc.base_name,
        group_slug=svc.group_slug,
        rub_price_kopecks=svc.rub_price_kopecks,
        in_stock=svc.in_stock,
    )


@router.post("/checkout", response_model=CheckoutResponseOut)
async def checkout_endpoint(
    body: CheckoutRequest,
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
):
    """
    Атомарный checkout через баланс. Идентичен button «💳 Купить» в боте.

    После создания ShopOrder (status=paid) NS-доставка запускается
    фоновым воркером (shop_delivery_poll каждые 60s). Inline-runner
    из FastAPI потока запускать нельзя (другая loop) — Mini App просто
    poll'ит `/orders/{id}` пока не появятся pins.
    """
    _, user = auth
    async with session_factory()() as session:
        result = await attempt_checkout_via_balance(
            session, user_id=user.id, ns_service_id=body.ns_service_id,
        )
        if result.outcome == CheckoutOutcome.OK:
            await session.commit()
        else:
            await session.rollback()

    if result.outcome == CheckoutOutcome.OK:
        return CheckoutResponseOut(
            outcome="ok",
            order_id=result.order.id,
            new_balance_kopecks=result.user_after_debit.balance_kopecks,
        )
    if result.outcome == CheckoutOutcome.INSUFFICIENT_BALANCE:
        return CheckoutResponseOut(
            outcome="insufficient_balance",
            need_kopecks=result.need_kopecks,
            have_kopecks=result.have_kopecks,
            deficit_kopecks=result.deficit_kopecks,
        )
    # Остальные outcomes — отдаём по простому
    return CheckoutResponseOut(outcome=result.outcome.value)


@router.get("/orders", response_model=OrderListResponse)
async def orders_list_endpoint(
    page: int = 0,
    page_size: int = 20,
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
):
    _, user = auth
    page_size = max(1, min(page_size, 100))
    offset = max(0, page) * page_size
    async with session_factory()() as session:
        orders, total = await list_user_orders(
            session, user_id=user.id, limit=page_size, offset=offset,
        )
    return OrderListResponse(
        orders=[_order_to_out(o) for o in orders],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/orders/{order_id}", response_model=OrderOut)
async def order_card_endpoint(
    order_id: int,
    auth: tuple[WebAppInitData, ShopUser] = Depends(current_webapp_user),
):
    _, user = auth
    async with session_factory()() as session:
        order = await get_shop_order(session, order_id)
    if order is None or order.user_id != user.id:
        raise HTTPException(404, "order not found")
    return _order_to_out(order)


def _order_to_out(order) -> OrderOut:
    pins = None
    if order.pins_json:
        try:
            pins = json.loads(order.pins_json)
        except json.JSONDecodeError:
            pins = None
    return OrderOut(
        id=order.id,
        ns_service_id=order.ns_service_id,
        ns_service_name=order.ns_service_name,
        total_rub_kopecks=order.total_rub_kopecks,
        status=order.status,
        created_at=order.created_at.isoformat() if order.created_at else "",
        delivered_at=order.delivered_at.isoformat() if order.delivered_at else None,
        pins=pins,
        error=order.error,
    )
