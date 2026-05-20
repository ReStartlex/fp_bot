from __future__ import annotations

from src.mapping.suggest import suggest_ns_services
from src.ns.models import Category, Service, StockResponse


def _stock() -> StockResponse:
    return StockResponse(categories=[
        Category(
            category_id=1,
            category_name="Blizzard Gift Card",
            services=[
                Service(
                    service_id=196,
                    service_name="Blizzard Gift Card | US | 5 USD",
                    price=4.92,
                    currency="USD",
                    in_stock=500,
                ),
                Service(
                    service_id=197,
                    service_name="Blizzard Gift Card | EU | 5 EUR",
                    price=5.10,
                    currency="USD",
                    in_stock=100,
                ),
            ],
        ),
        Category(
            category_id=2,
            category_name="Steam",
            services=[
                Service(
                    service_id=300,
                    service_name="Steam Gift Card | US | 5 USD",
                    price=4.80,
                    currency="USD",
                    in_stock=10,
                )
            ],
        ),
    ])


def test_suggest_ns_services_prefers_matching_brand_currency_and_amount():
    suggestions = suggest_ns_services(
        lot_title="Подарочная карта Battle.net 5 USD (США)",
        stock=_stock(),
    )

    assert suggestions
    assert suggestions[0].service_id == 196


def test_suggest_ns_services_keeps_currency_as_strong_signal():
    suggestions = suggest_ns_services(
        lot_title="Подарочная карта Battle.net 5 EUR (EU)",
        stock=_stock(),
    )

    assert suggestions
    assert suggestions[0].service_id == 197
