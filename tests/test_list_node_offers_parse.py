"""
P0-1 Фаза B (этап B2): парсер trade-страницы /lots/{node}/trade.

Зафиксированы РЕАЛЬНЫЕ (урезанные) ответы FunPay в
tests/fixtures/funpay/trade_node_*.html. Проверяем, что list_node_offers
извлекает offer_id + price (data-s, бывает дробной) + amount (tc-amount)
+ title. Это контракт для snapshot-sync (B3): сравнить состояние ноды с
target БЕЗ per-lot offerEdit GET'ов.

Если FunPay сменит вёрстку trade-страницы — тест упадёт здесь, а не тихо
в проде.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.funpay.admin_http import (
    FunPayAdminClient,
    _parse_offer_amount,
    _parse_offer_price,
)

FIX = Path(__file__).parent / "fixtures" / "funpay"


def _admin() -> FunPayAdminClient:
    return FunPayAdminClient(golden_key="x", phpsessid=None)


class _Resp:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200
        self.url = "https://funpay.com/lots/1086/trade"
        self.headers: dict = {}


def _patch_get(admin: FunPayAdminClient, html: str) -> None:
    admin._sync_get = lambda url: _Resp(html)  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_parse_steam_node_offers():
    html = (FIX / "trade_node_steam_1086.html").read_text(encoding="utf-8")
    admin = _admin()
    _patch_get(admin, html)

    offers = await admin.list_node_offers(1086)
    by_id = {o["offer_id"]: o for o in offers}

    assert len(offers) == 4
    assert by_id[70669637]["price"] == 8122.0
    assert by_id[70669637]["amount"] == 23
    assert "100" in by_id[70669637]["title"]
    assert by_id[70669636]["price"] == 4061.0
    assert by_id[70669636]["amount"] == 100
    assert by_id[70669635]["price"] == 2058.0
    assert by_id[70669635]["amount"] == 81
    assert by_id[70669634]["price"] == 2004.0
    assert by_id[70669634]["amount"] == 5


@pytest.mark.asyncio
async def test_parse_apple_node_offers_with_fractional_price():
    html = (FIX / "trade_node_apple_1316.html").read_text(encoding="utf-8")
    admin = _admin()
    _patch_get(admin, html)

    offers = await admin.list_node_offers(1316)
    by_id = {o["offer_id"]: o for o in offers}

    assert len(offers) == 5
    assert by_id[70670373]["price"] == 3549.0
    assert by_id[70670373]["amount"] == 10
    # дробная цена не должна терять копейки (data-s="439.04")
    assert by_id[70670366]["price"] == pytest.approx(439.04)
    assert by_id[70670368]["price"] == pytest.approx(879.0)


@pytest.mark.asyncio
async def test_offers_have_offer_id_and_active_present():
    html = (FIX / "trade_node_steam_1086.html").read_text(encoding="utf-8")
    admin = _admin()
    _patch_get(admin, html)
    offers = await admin.list_node_offers(1086)
    # trade-страница в наблюдаемой вёрстке = только активные офферы:
    # присутствие = active (True), не None.
    assert all(o["active"] is True for o in offers)
    assert all(o["offer_id"] > 0 for o in offers)


def test_parse_helpers_edge_cases():
    from bs4 import BeautifulSoup

    # data-s отсутствует — берём из текста
    el = BeautifulSoup(
        '<a class="tc-item"><div class="tc-price"><div>1 234 '
        '<span class="unit">₽</span></div></div>'
        '<div class="tc-amount">7</div></a>',
        "html.parser",
    ).select_one("a.tc-item")
    assert _parse_offer_price(el) == 1234.0
    assert _parse_offer_amount(el) == 7

    # нет tc-price / tc-amount вовсе → None
    bare = BeautifulSoup('<a class="tc-item"></a>', "html.parser").select_one("a")
    assert _parse_offer_price(bare) is None
    assert _parse_offer_amount(bare) is None
