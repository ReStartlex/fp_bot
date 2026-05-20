"""Подбор похожих NS-услуг для нового FunPay-лота."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from src.ns.models import Service, StockResponse


@dataclass(frozen=True)
class NsSuggestion:
    service_id: int
    service_name: str
    price: float
    currency: str
    in_stock: int
    score: int


_WEAK_TOKENS = {
    "gift",
    "card",
    "карта",
    "подарочная",
    "автовыдача",
    "auto",
    "delivery",
}


def _norm(value: str | None) -> str:
    raw = (value or "").lower().replace("ё", "е")
    raw = raw.replace("battle.net", "battle net")
    return " ".join(re.findall(r"[a-zа-я0-9]+", raw))


def _tokens(value: str | None) -> set[str]:
    return {
        token for token in _norm(value).split()
        if (len(token) > 1 or token.isdigit()) and token not in _WEAK_TOKENS
    }


def _iter_services(stock: StockResponse) -> Iterable[tuple[str, Service]]:
    for category in stock.categories:
        category_name = category.category_name or ""
        for service in category.services:
            yield category_name, service


def _score(lot_title: str | None, category_name: str, service: Service) -> int:
    title_norm = _norm(lot_title)
    service_text = f"{category_name} {service.service_name}"
    service_norm = _norm(service_text)
    if not title_norm or not service_norm:
        return 0

    score = 0
    if title_norm in service_norm or service_norm in title_norm:
        score += 120

    common = _tokens(lot_title) & _tokens(service_text)
    score += len(common) * 10
    score += sum(15 for token in common if token.isdigit())
    score += sum(20 for token in common if token in {"usd", "eur", "try", "rub"})
    if service.in_stock and service.in_stock > 0:
        score += 5
    return score


def suggest_ns_services(
    *,
    lot_title: str | None,
    stock: StockResponse,
    limit: int = 3,
    min_score: int = 20,
) -> list[NsSuggestion]:
    scored: list[NsSuggestion] = []
    for category_name, service in _iter_services(stock):
        score = _score(lot_title, category_name, service)
        if score < min_score:
            continue
        scored.append(
            NsSuggestion(
                service_id=service.service_id,
                service_name=service.service_name,
                price=float(service.price),
                currency=service.currency or "USD",
                in_stock=int(service.in_stock or 0),
                score=score,
            )
        )
    scored.sort(key=lambda item: (item.score, item.in_stock, -item.price), reverse=True)
    return scored[:limit]
