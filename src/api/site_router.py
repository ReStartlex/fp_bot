"""
Авторизованный API сайта neurodrop.ru — `/api/site/*`.

Авторизация: Telegram **Login Widget** → серверная сессия. Токен сессии
принимается двумя способами (что удобнее фронту):
  * cookie `nd_session` (httpOnly) — если браузер ходит в API напрямую;
  * заголовок `Authorization: Bearer <token>` — если Next.js проксирует
    запросы server-side и сам владеет cookie в браузере.

Логин возвращает токен и в теле ответа (`token`), и ставит cookie —
поэтому схема работает при любой топологии (прямой доступ или прокси
через Next.js, где API закрыт на 127.0.0.1).

Аккаунт единый с ботом: тот же `ShopUser` (по telegram_user_id), тот же
баланс и рефералка. Сайт и Telegram — один кошелёк.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Response,
    status,
)
from loguru import logger
from pydantic import BaseModel, Field

from src.api.telegram_login import LoginAuthError, verify_login_widget
from src.api.web_session import (
    SessionError,
    issue_session_token,
    verify_session_token,
)
from src.config import Settings, get_settings
from src.config_runtime import get_shop_referral_percent
from src.db.models import ShopUser
from src.db.session import session_factory
from src.shop.checkout import CheckoutOutcome, attempt_checkout_via_balance
from src.shop.payments.cryptobot import CryptoBotClient, CryptoBotError
from src.shop.repo import (
    create_topup_payment,
    get_balance_stats,
    get_or_create_user,
    get_referral_stats,
    get_shop_order,
    get_user_by_tg,
    list_user_orders,
)


router = APIRouter(prefix="/api/site", tags=["site-auth"])

COOKIE_NAME = "nd_session"


# ─── Schemas ───────────────────────────────────────────────────────


class SiteMe(BaseModel):
    user_id: int
    telegram_user_id: int
    username: str | None
    first_name: str | None
    photo_url: str | None = None
    balance_kopecks: int
    total_earned_kopecks: int
    total_spent_kopecks: int
    invited_count: int
    earned_via_referrals_kopecks: int
    referral_percent: float


class LoginResponse(SiteMe):
    # Токен дублируется в теле, чтобы Next.js мог сам поставить cookie
    # в браузере (когда API закрыт на localhost и недоступен напрямую).
    token: str


class TopupRequest(BaseModel):
    amount_rub: float = Field(..., gt=0)


class TopupResponse(BaseModel):
    pay_url: str
    invoice_id: int
    amount_kopecks: int


class SiteCheckoutRequest(BaseModel):
    ns_service_id: int


class SiteCheckoutResponse(BaseModel):
    outcome: str
    order_id: int | None = None
    new_balance_kopecks: int | None = None
    need_kopecks: int | None = None
    have_kopecks: int | None = None
    deficit_kopecks: int | None = None


class SiteOrderOut(BaseModel):
    id: int
    ns_service_id: int
    ns_service_name: str
    total_rub_kopecks: int
    status: str
    created_at: str
    delivered_at: str | None
    pins: list | None = None
    error: str | None = None


class SiteOrdersPage(BaseModel):
    orders: list[SiteOrderOut]
    total: int
    page: int
    page_size: int


# ─── Auth dependency ───────────────────────────────────────────────


def _extract_token(
    cookie_token: str | None,
    authorization: str | None,
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return cookie_token


async def current_site_user(
    nd_session: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> ShopUser:
    """Достаёт ShopUser из session-токена (cookie или Bearer). 401 если нет/битый."""
    token = _extract_token(nd_session, authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    try:
        claims = verify_session_token(token, settings=settings)
    except SessionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"bad session: {exc}")
    async with session_factory()() as session:
        user = await get_user_by_tg(
            session, telegram_user_id=claims.telegram_user_id
        )
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    if user.blocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user is blocked")
    return user


def _shop_bot_token(settings: Settings) -> str:
    secret = settings.shop_telegram_bot_token
    if secret is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "shop bot token not configured"
        )
    return secret.get_secret_value()


async def _build_me(user: ShopUser, settings: Settings) -> dict[str, Any]:
    async with session_factory()() as session:
        bal = await get_balance_stats(session, user_id=user.id)
        ref = await get_referral_stats(session, user_id=user.id)
    referral_percent = await get_shop_referral_percent(settings)
    return dict(
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        username=user.telegram_username,
        first_name=user.first_name,
        balance_kopecks=user.balance_kopecks,
        total_earned_kopecks=bal.total_earned_kopecks,
        total_spent_kopecks=bal.total_spent_kopecks,
        invited_count=ref.invited_count,
        earned_via_referrals_kopecks=ref.total_earned_kopecks,
        referral_percent=referral_percent,
    )


# ─── Endpoints ─────────────────────────────────────────────────────


@router.post("/auth/telegram", response_model=LoginResponse)
async def auth_telegram(
    response: Response,
    payload: dict[str, Any] = Body(...),
    settings: Settings = Depends(get_settings),
):
    """
    Вход через Telegram Login Widget.

    Тело — ровно объект, который виджет отдаёт на фронте
    (id, first_name, username, photo_url, auth_date, hash, ...).
    Все поля идут в проверку подписи как есть.
    """
    if not settings.shop_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "shop disabled")
    bot_token = _shop_bot_token(settings)
    try:
        tg = verify_login_widget(
            payload,
            bot_token=bot_token,
            max_age_seconds=settings.site_login_max_age_seconds,
        )
    except LoginAuthError as exc:
        logger.debug(f"site login rejected: {exc}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"login invalid: {exc}")

    async with session_factory()() as session:
        user, _is_new = await get_or_create_user(
            session,
            telegram_user_id=tg.id,
            telegram_username=tg.username or None,
            first_name=tg.first_name or None,
        )
        await session.commit()
    if user.blocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user is blocked")

    token = issue_session_token(
        user_id=user.id, telegram_user_id=user.telegram_user_id, settings=settings,
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.site_session_ttl_seconds,
        httponly=True,
        secure=settings.site_cookie_secure,
        samesite="lax",
        path="/",
    )
    me = await _build_me(user, settings)
    me["photo_url"] = tg.photo_url or None
    return LoginResponse(token=token, **me)


@router.post("/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me", response_model=SiteMe)
async def site_me(
    user: ShopUser = Depends(current_site_user),
    settings: Settings = Depends(get_settings),
):
    return SiteMe(**await _build_me(user, settings))


@router.get("/orders", response_model=SiteOrdersPage)
async def site_orders(
    page: int = 0,
    page_size: int = 20,
    user: ShopUser = Depends(current_site_user),
):
    page_size = max(1, min(page_size, 100))
    offset = max(0, page) * page_size
    async with session_factory()() as session:
        orders, total = await list_user_orders(
            session, user_id=user.id, limit=page_size, offset=offset,
        )
    return SiteOrdersPage(
        orders=[_order_to_out(o) for o in orders],
        total=total, page=page, page_size=page_size,
    )


@router.get("/orders/{order_id}", response_model=SiteOrderOut)
async def site_order_card(
    order_id: int,
    user: ShopUser = Depends(current_site_user),
):
    async with session_factory()() as session:
        order = await get_shop_order(session, order_id)
    if order is None or order.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found")
    return _order_to_out(order)


@router.post("/checkout", response_model=SiteCheckoutResponse)
async def site_checkout(
    body: SiteCheckoutRequest,
    user: ShopUser = Depends(current_site_user),
):
    """
    Оплата товара с внутреннего баланса (как «💳 Купить» в боте/Mini App).
    NS-доставка запускается фоновым воркером shop_delivery_poll; фронт
    poll'ит /orders/{id} до появления pins.
    """
    async with session_factory()() as session:
        result = await attempt_checkout_via_balance(
            session, user_id=user.id, ns_service_id=body.ns_service_id,
        )
        if result.outcome == CheckoutOutcome.OK:
            await session.commit()
        else:
            await session.rollback()

    if result.outcome == CheckoutOutcome.OK:
        return SiteCheckoutResponse(
            outcome="ok",
            order_id=result.order.id,
            new_balance_kopecks=result.user_after_debit.balance_kopecks,
        )
    if result.outcome == CheckoutOutcome.INSUFFICIENT_BALANCE:
        return SiteCheckoutResponse(
            outcome="insufficient_balance",
            need_kopecks=result.need_kopecks,
            have_kopecks=result.have_kopecks,
            deficit_kopecks=result.deficit_kopecks,
        )
    return SiteCheckoutResponse(outcome=result.outcome.value)


@router.post("/topup", response_model=TopupResponse)
async def site_topup(
    body: TopupRequest,
    user: ShopUser = Depends(current_site_user),
    settings: Settings = Depends(get_settings),
):
    """
    Создаёт счёт CryptoBot на пополнение внутреннего баланса.

    Баланс зачислится автоматически фоновым CryptoBot-поллером (тот же,
    что и для бота) после оплаты счёта — фронт поллит /me до изменения
    баланса. Идемпотентность гарантирует UNIQUE(provider, invoice_id).
    """
    token = settings.cryptobot_api_token
    if token is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "crypto payment not configured"
        )
    rub = body.amount_rub
    if rub < settings.cryptobot_min_topup_rub:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"min topup is {settings.cryptobot_min_topup_rub} ₽",
        )
    if rub > settings.cryptobot_max_topup_rub:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"max topup is {settings.cryptobot_max_topup_rub} ₽",
        )
    amount_kopecks = int(round(rub * 100))

    cli = CryptoBotClient(
        api_token=token.get_secret_value(), testnet=settings.cryptobot_testnet,
    )
    try:
        invoice = await cli.create_invoice(
            amount_rub=Decimal(amount_kopecks) / Decimal(100),
            description=f"Пополнение баланса NeuroDrop ({rub:g} ₽)",
            payload=f"tg:{user.telegram_user_id}",
            expires_in=settings.cryptobot_invoice_ttl_seconds,
        )
    except CryptoBotError as exc:
        logger.warning(f"site topup createInvoice failed: {exc}")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "payment provider error"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"site topup createInvoice crashed: {exc}")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "payment provider unavailable"
        )

    async with session_factory()() as session:
        await create_topup_payment(
            session,
            user_id=user.id,
            provider="cryptobot",
            provider_invoice_id=str(invoice.invoice_id),
            amount_kopecks=amount_kopecks,
            notify_telegram_id=user.telegram_user_id,
        )
        await session.commit()

    return TopupResponse(
        pay_url=invoice.pay_url,
        invoice_id=invoice.invoice_id,
        amount_kopecks=amount_kopecks,
    )


def _order_to_out(order) -> SiteOrderOut:
    pins = None
    if order.pins_json:
        try:
            pins = json.loads(order.pins_json)
        except json.JSONDecodeError:
            pins = None
    return SiteOrderOut(
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
