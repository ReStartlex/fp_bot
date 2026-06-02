"""
Runtime-настройки, которые можно менять без перезапуска (из Telegram-бота).

Параметры хранятся в таблице `runtime_settings` (key/value). Если ключ
есть в БД — он переопределяет соответствующее поле в `Settings`. Если
нет — используется значение из `.env` (через `Settings`).

Все читалки берут БД при каждом вызове. Это дёшево (SQLite, одна строка),
зато гарантирует консистентность с настройками, которые юзер только что
поменял из бота — без необходимости держать инвалидируемый кэш.

Поддерживаемые ключи:
    global_markup_percent     - наценка по умолчанию (если у маппинга NULL)
    usd_rub_premium_percent   - премия к биржевому курсу USD/RUB
    funpay_stock_cap          - кап остатков, проставляемых на FunPay
    shop_markup_percent       - наценка для shop-каталога (Phase 1)
    shop_referral_percent     - % кэшбэка рефереру с покупки реферала
"""
from __future__ import annotations

from sqlalchemy import select

from src.config import Settings, get_settings
from src.db.models import RuntimeSetting
from src.db.session import session_factory


KEY_MARKUP = "global_markup_percent"
KEY_PREMIUM = "usd_rub_premium_percent"
KEY_STOCK_CAP = "funpay_stock_cap"
KEY_SHOP_MARKUP = "shop_markup_percent"
KEY_SHOP_REFERRAL = "shop_referral_percent"


async def _get_raw(key: str) -> str | None:
    async with session_factory()() as session:
        row = (await session.execute(
            select(RuntimeSetting).where(RuntimeSetting.key == key)
        )).scalar_one_or_none()
        return row.value if row is not None else None


async def _set_raw(key: str, value: str | None) -> None:
    """Set/unset. None или пустая строка — удалить override."""
    async with session_factory()() as session:
        row = (await session.execute(
            select(RuntimeSetting).where(RuntimeSetting.key == key)
        )).scalar_one_or_none()
        if value is None or value == "":
            if row is not None:
                await session.delete(row)
                await session.commit()
            return
        if row is None:
            row = RuntimeSetting(key=key, value=value)
            session.add(row)
        else:
            row.value = value
        await session.commit()


# ─────────── markup ───────────

async def get_global_markup_percent(settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    raw = await _get_raw(KEY_MARKUP)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return settings.markup_percent


async def set_global_markup_percent(value: float | None) -> None:
    if value is None:
        await _set_raw(KEY_MARKUP, None)
        return
    if value < 0 or value > 200:
        raise ValueError("markup_percent должен быть в диапазоне 0..200")
    await _set_raw(KEY_MARKUP, f"{value:.4f}")


# ─────────── premium ───────────

async def get_premium_percent(settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    raw = await _get_raw(KEY_PREMIUM)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return settings.usd_rub_premium_percent


async def set_premium_percent(value: float | None) -> None:
    if value is None:
        await _set_raw(KEY_PREMIUM, None)
        return
    if value < 0 or value > 50:
        raise ValueError("usd_rub_premium_percent должен быть в диапазоне 0..50")
    await _set_raw(KEY_PREMIUM, f"{value:.4f}")


# ─────────── stock cap ───────────

async def get_stock_cap(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    raw = await _get_raw(KEY_STOCK_CAP)
    if raw is not None:
        try:
            return int(float(raw))
        except ValueError:
            pass
    return settings.funpay_stock_cap


async def set_stock_cap(value: int | None) -> None:
    if value is None:
        await _set_raw(KEY_STOCK_CAP, None)
        return
    if value < 1 or value > 100000:
        raise ValueError("stock_cap должен быть в диапазоне 1..100000")
    await _set_raw(KEY_STOCK_CAP, str(int(value)))


async def get_overrides_snapshot() -> dict[str, str | None]:
    """Удобно для /status и /settings — вернуть все активные оверрайды."""
    return {
        KEY_MARKUP: await _get_raw(KEY_MARKUP),
        KEY_PREMIUM: await _get_raw(KEY_PREMIUM),
        KEY_STOCK_CAP: await _get_raw(KEY_STOCK_CAP),
        KEY_SHOP_MARKUP: await _get_raw(KEY_SHOP_MARKUP),
        KEY_SHOP_REFERRAL: await _get_raw(KEY_SHOP_REFERRAL),
    }


# ─────────── shop_markup ───────────

async def get_shop_markup_percent(settings: Settings | None = None) -> float:
    """
    Наценка для shop-каталога. Приоритет: runtime override → settings.shop_markup_percent.
    Не путать с global_markup_percent (FunPay-маркапы) — это разные параметры,
    разная аудитория, разная экономика (FunPay 5% net, shop 8% gross).
    """
    settings = settings or get_settings()
    raw = await _get_raw(KEY_SHOP_MARKUP)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return settings.shop_markup_percent


async def set_shop_markup_percent(value: float | None) -> None:
    if value is None:
        await _set_raw(KEY_SHOP_MARKUP, None)
        return
    if value < 0 or value > 100:
        raise ValueError("shop_markup_percent должен быть в диапазоне 0..100")
    await _set_raw(KEY_SHOP_MARKUP, f"{value:.4f}")


# ─────────── shop_referral ───────────

async def get_shop_referral_percent(settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    raw = await _get_raw(KEY_SHOP_REFERRAL)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return settings.shop_referral_percent


async def set_shop_referral_percent(value: float | None) -> None:
    if value is None:
        await _set_raw(KEY_SHOP_REFERRAL, None)
        return
    if value < 0 or value > 100:
        raise ValueError("shop_referral_percent должен быть в диапазоне 0..100")
    await _set_raw(KEY_SHOP_REFERRAL, f"{value:.4f}")
