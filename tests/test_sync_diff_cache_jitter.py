"""
P0-1 фаза A: джиттер TTL diff-cache против залпа GET (burst).

Без джиттера после общего цикла у всех лотов last_synced_at ≈ одно время
→ TTL истекает разом → залп GET → 429-шторм → «maximum number of running
instances». Джиттер даёт каждому лоту детерминированный сдвиг 0..jitter,
растягивая переоткалибровку по окну.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from src.mapping.rules import PricingResult
from src.ns.models import Service  # noqa: F401 (parity со стилем модуля)
from src.sync.stock_sync import _is_cache_hit, _lot_ttl_jitter

try:
    from src.mapping.rules import Currency
    _RUB = Currency.RUB
except Exception:  # pragma: no cover
    _RUB = "RUB"  # type: ignore[assignment]


def _target(price: float = 100.0, stock: int = 50) -> PricingResult:
    return PricingResult(
        ns_price_usd=1.0, fx_rate=75.0, markup_percent=10.0,
        price_target=price, stock=stock, currency=_RUB,
    )


def _mapping(lot_id: int, age_seconds: float, *, price=100.0, stock=50, now=None):
    now = now or datetime.utcnow()
    return SimpleNamespace(
        funpay_lot_id=lot_id,
        last_synced_at=now - timedelta(seconds=age_seconds),
        last_synced_price=price,
        last_synced_stock=stock,
        last_synced_active=(stock > 0),
    )


# ───────────── _lot_ttl_jitter ─────────────

def test_jitter_within_range_and_deterministic():
    for lot_id in (1, 10, 100, 69300023, 70670053):
        j1 = _lot_ttl_jitter(lot_id, 60)
        j2 = _lot_ttl_jitter(lot_id, 60)
        assert j1 == j2                 # детерминирован
        assert 0 <= j1 <= 60            # в диапазоне


def test_jitter_zero_disables():
    assert _lot_ttl_jitter(12345, 0) == 0


def test_jitter_spreads_lot_ids():
    # разные lot_id дают (как правило) разный сдвиг — залп растягивается
    vals = {_lot_ttl_jitter(i, 60) for i in range(1, 62)}
    assert len(vals) > 30


# ───────────── _is_cache_hit с джиттером ─────────────

def test_jitter_extends_effective_ttl():
    now = datetime.utcnow()
    # lot_id=10 → jitter=10 → effective_ttl = 120+10 = 130
    m = _mapping(10, age_seconds=125, now=now)
    # без джиттера: 125 >= 120 → miss
    assert _is_cache_hit(mapping=m, target=_target(), ttl_seconds=120, now=now) is False
    # с джиттером 60: 125 < 130 → всё ещё hit
    assert _is_cache_hit(
        mapping=m, target=_target(), ttl_seconds=120, now=now, jitter_seconds=60
    ) is True


def test_jitter_different_lots_expire_at_different_ages():
    now = datetime.utcnow()
    # один и тот же возраст 125с, но разные lot_id → разный исход
    m_short = _mapping(1, age_seconds=125, now=now)   # jitter=1 → ttl 121 → miss
    m_long = _mapping(10, age_seconds=125, now=now)   # jitter=10 → ttl 130 → hit
    assert _is_cache_hit(
        mapping=m_short, target=_target(), ttl_seconds=120, now=now, jitter_seconds=60
    ) is False
    assert _is_cache_hit(
        mapping=m_long, target=_target(), ttl_seconds=120, now=now, jitter_seconds=60
    ) is True


def test_no_jitter_preserves_old_behavior():
    now = datetime.utcnow()
    m = _mapping(10, age_seconds=125, now=now)
    # jitter_seconds=0 (default) → ровно как раньше: 125 >= 120 → miss
    assert _is_cache_hit(mapping=m, target=_target(), ttl_seconds=120, now=now) is False
    m2 = _mapping(10, age_seconds=119, now=now)
    assert _is_cache_hit(mapping=m2, target=_target(), ttl_seconds=120, now=now) is True
