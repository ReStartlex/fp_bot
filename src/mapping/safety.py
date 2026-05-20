"""Проверки перед созданием FunPay -> NS mapping."""
from __future__ import annotations

import re


_CURRENCIES = {"usd", "eur", "try", "rub", "kzt", "uah"}


def _norm(value: str | None) -> str:
    raw = (value or "").lower().replace("ё", "е")
    raw = raw.replace("battle.net", "battle net")
    return " ".join(re.findall(r"[a-zа-я0-9]+", raw))


def _tokens(value: str | None) -> set[str]:
    return set(_norm(value).split())


def mapping_risk_warnings(funpay_title: str | None, ns_title: str | None) -> list[str]:
    fp_tokens = _tokens(funpay_title)
    ns_tokens = _tokens(ns_title)
    if not fp_tokens or not ns_tokens:
        return []

    warnings: list[str] = []
    fp_numbers = {t for t in fp_tokens if t.isdigit()}
    ns_numbers = {t for t in ns_tokens if t.isdigit()}
    if fp_numbers and ns_numbers and fp_numbers.isdisjoint(ns_numbers):
        warnings.append(
            f"номинал отличается: FunPay {sorted(fp_numbers)} vs NS {sorted(ns_numbers)}"
        )

    fp_currency = fp_tokens & _CURRENCIES
    ns_currency = ns_tokens & _CURRENCIES
    if fp_currency and ns_currency and fp_currency.isdisjoint(ns_currency):
        warnings.append(
            f"валюта отличается: FunPay {sorted(fp_currency)} vs NS {sorted(ns_currency)}"
        )

    strong_common = {
        t for t in (fp_tokens & ns_tokens)
        if len(t) >= 4 and t not in _CURRENCIES
    }
    if not strong_common and fp_numbers.isdisjoint(ns_numbers):
        warnings.append("названия почти не похожи")

    return warnings
