"""
Тесты для diff-based sync_stock cache (главная оптимизация против 429).

Покрываем:
  * _is_cache_hit:
    - True, если target == cache и cache свежий
    - False, если cache отсутствует (первый прогон, миграция)
    - False, если cache протух (старше TTL)
    - False, если price/stock/active не совпадают
    - Допуск 0.005 на цену (защита от floating-point round-trip)
  * _compute_target_quickly:
    - Возвращает PricingResult без FunPay-запросов
    - Возвращает None если ns_service отсутствует
  * _find_mapping_id_for_decision:
    - Находит mapping.id по decision.funpay_lot_id
    - Возвращает None если не нашёл
  * update_mapping_last_synced (БД):
    - Записывает price/stock/active/at в БД
    - Перезаписывает существующие значения

Не покрываем full run_sync_once здесь (это тяжёлая интеграция,
требует мокать FunPay+NS+session_factory — не вписывается в unit).
Главная логика — _is_cache_hit и update_mapping_last_synced —
покрыта на 100%.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Currency
from src.db.models import Base
from src.mapping.rules import PricingResult
from src.sync.stock_sync import (
    LotSyncDecision,
    _compute_target_quickly,
    _find_mapping_id_for_decision,
    _is_cache_hit,
)


@pytest.fixture()
async def db_factory():
    """In-memory sqlite + Mapping table готовый к использованию."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_target(*, price: float = 100.0, stock: int = 5) -> PricingResult:
    return PricingResult(
        ns_price_usd=1.0,
        fx_rate=73.0,
        markup_percent=5.0,
        price_target=price,
        stock=stock,
        currency=Currency.RUB,
    )


def _make_mapping(
    *,
    last_price: float | None = None,
    last_stock: int | None = None,
    last_active: bool | None = None,
    last_at: datetime | None = None,
    mapping_id: int = 1,
    funpay_lot_id: int = 12345,
) -> SimpleNamespace:
    """Лёгкий mock Mapping с теми полями, что трогает diff-cache."""
    return SimpleNamespace(
        id=mapping_id,
        funpay_lot_id=funpay_lot_id,
        ns_service_id=99,
        label="test lot",
        markup_percent=None,
        stock_cap=None,
        group_id=None,
        last_synced_price=last_price,
        last_synced_stock=last_stock,
        last_synced_active=last_active,
        last_synced_at=last_at,
    )


# ─────────────── _is_cache_hit ───────────────


def test_cache_hit_when_target_matches_and_fresh():
    """Главный happy-path: target == cache, cache моложе TTL → True."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    mapping = _make_mapping(
        last_price=100.0, last_stock=5, last_active=True,
        last_at=now - timedelta(seconds=60),  # 1 min ago, TTL=300
    )
    target = _make_target(price=100.0, stock=5)
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300, now=now
    ) is True


def test_cache_miss_when_cache_empty_first_run():
    """Все last_synced_* = NULL → cache miss (первый прогон/миграция)."""
    mapping = _make_mapping()  # все NULL
    target = _make_target()
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300
    ) is False


def test_cache_miss_when_only_last_at_is_null():
    """Если последний sync был, но _at NULL (теоретическая аномалия) — miss."""
    mapping = _make_mapping(
        last_price=100.0, last_stock=5, last_active=True,
        last_at=None,
    )
    target = _make_target()
    assert _is_cache_hit(mapping=mapping, target=target, ttl_seconds=300) is False


def test_cache_miss_when_ttl_expired():
    """Cache старше TTL → miss (защита от рассинхрона)."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    mapping = _make_mapping(
        last_price=100.0, last_stock=5, last_active=True,
        last_at=now - timedelta(seconds=400),  # старше 300
    )
    target = _make_target()
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300, now=now
    ) is False


def test_cache_miss_when_price_differs():
    now = datetime(2026, 5, 24, 12, 0, 0)
    mapping = _make_mapping(
        last_price=100.0, last_stock=5, last_active=True,
        last_at=now - timedelta(seconds=60),
    )
    target = _make_target(price=110.0)  # цена изменилась
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300, now=now
    ) is False


def test_cache_miss_when_stock_differs():
    now = datetime(2026, 5, 24, 12, 0, 0)
    mapping = _make_mapping(
        last_price=100.0, last_stock=5, last_active=True,
        last_at=now - timedelta(seconds=60),
    )
    target = _make_target(stock=10)
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300, now=now
    ) is False


def test_cache_miss_when_active_differs():
    """target.stock=0 → active=False; cache last_active=True → miss."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    mapping = _make_mapping(
        last_price=100.0, last_stock=5, last_active=True,
        last_at=now - timedelta(seconds=60),
    )
    target = _make_target(stock=0)  # выкл лота
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300, now=now
    ) is False


def test_cache_hit_price_within_floating_point_tolerance():
    """100.003 vs 100.000 — должно быть cache hit (допуск 0.005)."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    mapping = _make_mapping(
        last_price=100.000, last_stock=5, last_active=True,
        last_at=now - timedelta(seconds=60),
    )
    target = _make_target(price=100.003)
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300, now=now
    ) is True


def test_cache_miss_price_outside_tolerance():
    """100.0 vs 101.0 — разница больше допуска → miss.

    Сначала возникал соблазн тестировать тут разницу 0.01 (just outside
    of tolerance), но для RUB `round_price()` округляет до целого,
    и 100.01 ~ 100.00 → cache hit. Реалистичный кейс изменения цены
    — это >=1 RUB, и тут уже точно miss.
    """
    now = datetime(2026, 5, 24, 12, 0, 0)
    mapping = _make_mapping(
        last_price=100.00, last_stock=5, last_active=True,
        last_at=now - timedelta(seconds=60),
    )
    target = _make_target(price=101.0)  # на 1 RUB больше
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300, now=now
    ) is False


def test_cache_hit_exact_boundary_at_ttl():
    """Cache ровно на границе TTL: now - last_at == TTL → miss (>=, не >)."""
    now = datetime(2026, 5, 24, 12, 0, 0)
    mapping = _make_mapping(
        last_price=100.0, last_stock=5, last_active=True,
        last_at=now - timedelta(seconds=300),  # ровно 300
    )
    target = _make_target()
    assert _is_cache_hit(
        mapping=mapping, target=target, ttl_seconds=300, now=now
    ) is False


# ─────────────── _compute_target_quickly ───────────────


def test_compute_target_quickly_returns_none_when_ns_service_missing():
    """Если NS-каталог не нашёл сервис — fast-path не применим."""
    mapping = _make_mapping()
    settings = SimpleNamespace(
        funpay_currency=Currency.RUB,
        usd_rub_premium_percent=0.0,
        funpay_withdrawal_fee_percent=0.0,
        funpay_commission_percent=0.0,
    )
    result = _compute_target_quickly(
        ns_service=None,
        mapping=mapping,
        settings=settings,
        fx_rate=73.0,
        effective_markup=5.0,
        effective_stock_cap=100,
        group=None,
    )
    assert result is None


def test_compute_target_quickly_does_not_call_funpay():
    """Smoke: успешный compute БЕЗ единого FunPay-вызова.
    (Гарантировано тем, что мы передаём None в funpay_client — этот
    helper его вообще не принимает в аргументах.)"""
    from src.ns.models import Service

    mapping = _make_mapping()
    ns_service = Service(
        service_id=99,
        service_name="Test Service",
        price=1.5,
        currency="USD",
        in_stock=10,
    )
    settings = SimpleNamespace(
        funpay_currency=Currency.RUB,
        usd_rub_premium_percent=0.0,
        funpay_withdrawal_fee_percent=0.0,
        funpay_commission_percent=0.0,
    )
    result = _compute_target_quickly(
        ns_service=ns_service,
        mapping=mapping,
        settings=settings,
        fx_rate=73.0,
        effective_markup=5.0,
        effective_stock_cap=100,
        group=None,
    )
    assert result is not None
    assert isinstance(result, PricingResult)
    assert result.stock > 0


# ─────────────── _find_mapping_id_for_decision ───────────────


def test_find_mapping_id_by_funpay_lot_id():
    mappings = [
        _make_mapping(mapping_id=1, funpay_lot_id=111),
        _make_mapping(mapping_id=2, funpay_lot_id=222),
        _make_mapping(mapping_id=3, funpay_lot_id=333),
    ]
    decision = LotSyncDecision(
        funpay_lot_id=222,
        ns_service_id=99, label=None,
        current_price=100.0, target=_make_target(),
        will_update_price=False, will_update_stock=False,
        will_activate=False, will_deactivate=False,
    )
    assert _find_mapping_id_for_decision(mappings, decision) == 2


def test_find_mapping_id_returns_none_when_no_match():
    mappings = [_make_mapping(mapping_id=1, funpay_lot_id=111)]
    decision = LotSyncDecision(
        funpay_lot_id=999,  # not present
        ns_service_id=99, label=None,
        current_price=100.0, target=_make_target(),
        will_update_price=False, will_update_stock=False,
        will_activate=False, will_deactivate=False,
    )
    assert _find_mapping_id_for_decision(mappings, decision) is None


# ─────────────── update_mapping_last_synced (БД) ───────────────


@pytest.mark.asyncio
async def test_update_mapping_last_synced_persists_to_db(db_factory):
    """В БД должна попасть запись (price, stock, active, at=now)."""
    from sqlalchemy import select
    from src.db.models import Mapping
    from src.db.repo import upsert_mapping, update_mapping_last_synced

    async with db_factory() as session:
        mapping = await upsert_mapping(
            session,
            funpay_lot_id=42,
            ns_service_id=99,
            enabled=True,
            label="t",
        )
        await session.commit()
        mid = mapping.id

    async with db_factory() as session:
        await update_mapping_last_synced(
            session,
            mapping_id=mid,
            price=123.45,
            stock=7,
            active=True,
        )
        await session.commit()

    async with db_factory() as session:
        m = (await session.execute(
            select(Mapping).where(Mapping.id == mid)
        )).scalar_one()
        assert m.last_synced_price == pytest.approx(123.45, rel=1e-6)
        assert m.last_synced_stock == 7
        assert m.last_synced_active is True
        assert m.last_synced_at is not None


def test_cache_update_for_no_action_happens_before_continue():
    """
    Регрессионный тест против бага, обнаруженного в проде после
    деплоя 7cf15e6: блок «обновить cache после verified no-action»
    был ПОСЛЕ `continue`, поэтому никогда не выполнялся для лотов
    без actions (а это 46 из 47 в стабильном состоянии). В логах:
        Sync done: checked=46, unchanged=1, ...
    т.е. cache не наполнялся, fast-path был бесполезен.

    Проверяем построчно: в блоке обработки `decisions` после
    `if not actions:` должна быть строка `pending_cache_updates.append`
    ДО следующего `continue`.

    Реализация: смотрим line-by-line. Когда видим `if not actions:`,
    идём вниз пока не встретим `continue` (выход из блока) или
    `pending_cache_updates.append` (cache update). Первое из двух
    должно быть append, иначе — мёртвый код после continue.
    """
    from pathlib import Path
    src = Path(__file__).parent.parent / "src" / "sync" / "stock_sync.py"
    lines = src.read_text(encoding="utf-8").splitlines()

    # Ищем строку `if not actions:` именно в run-цикле декидов (отступ ~12 пробелов).
    # Точно: ищем `if not actions:` БЕЗ inversion (т.е. без 'and'), на уровне `for decision in decisions`.
    found_block = False
    cache_seen_before_continue = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped != "if not actions:":
            continue
        # Это потенциальный наш блок. Найдём следующий `continue` или append.
        for j in range(i + 1, min(i + 30, len(lines))):
            inner = lines[j].strip()
            if inner.startswith("pending_cache_updates.append"):
                found_block = True
                cache_seen_before_continue = True
                break
            if inner == "continue":
                found_block = True
                break
        if found_block:
            break

    assert found_block, "не найден блок `if not actions:` в src/sync/stock_sync.py"
    assert cache_seen_before_continue, (
        "БАГ: в no-action блоке `pending_cache_updates.append` "
        "находится ПОСЛЕ `continue` (или вообще отсутствует). "
        "Это значит cache никогда не наполнится для лотов "
        "без actions (46 из 47 в стабильном состоянии), "
        "и diff-fast-path будет бесполезен."
    )


@pytest.mark.asyncio
async def test_update_mapping_last_synced_overwrites_previous_values(db_factory):
    """Повторный вызов перезаписывает значения."""
    from sqlalchemy import select
    from src.db.models import Mapping
    from src.db.repo import upsert_mapping, update_mapping_last_synced

    async with db_factory() as session:
        mapping = await upsert_mapping(
            session, funpay_lot_id=42, ns_service_id=99,
            enabled=True, label="t",
        )
        await session.commit()
        mid = mapping.id

    async with db_factory() as session:
        await update_mapping_last_synced(
            session, mapping_id=mid,
            price=100.0, stock=5, active=True,
        )
        await session.commit()
    async with db_factory() as session:
        await update_mapping_last_synced(
            session, mapping_id=mid,
            price=200.0, stock=0, active=False,
        )
        await session.commit()

    async with db_factory() as session:
        m = (await session.execute(
            select(Mapping).where(Mapping.id == mid)
        )).scalar_one()
        assert m.last_synced_price == pytest.approx(200.0)
        assert m.last_synced_stock == 0
        assert m.last_synced_active is False
