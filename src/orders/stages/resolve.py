"""
Стадия resolve: сопоставление FunPay-заказа с маппингом и chat_id.

Вынесено из `processor.py` (P1-1) без изменения поведения. Содержит
матчинг заказа без lot_id (вето валюты/номинала, word-boundary, scoring,
AmbiguousMatch) и восстановление chat_id по buyer_username.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from src.config import Settings, get_settings
from src.db.models import KnownLot, Mapping
from src.db.session import session_factory
from src.funpay.client import FunPayClient
from src.orders.events import FunPayOrderEvent


def _norm_text(value: str | None) -> str:
    raw = (value or "").lower()
    raw = raw.replace("ё", "е")
    return " ".join(re.findall(r"[a-zа-я0-9]+", raw))


_WEAK_MATCH_TOKENS = {
    "gift",
    "card",
    "карта",
    "подарочная",
    "автовыдача",
    "auto",
    "delivery",
}


def _text_tokens(value: str | None) -> set[str]:
    tokens = set(_norm_text(value).split())
    return {
        t for t in tokens
        if (len(t) > 1 or t.isdigit()) and t not in _WEAK_MATCH_TOKENS
    }


# Валютные токены для вето-проверки (см. _tokens_conflict). Набор шире,
# чем в mapping/safety.py — здесь цена ошибки выше (реальная покупка).
_CURRENCY_TOKENS = {
    "usd", "eur", "try", "rub", "kzt", "uah", "gbp", "pln",
    "brl", "inr", "ars", "idr", "myr", "php", "thb", "vnd",
}

# Числовые токены длиннее — это скорее ID/артикул, чем номинал.
_NOMINAL_MAX_LEN = 5


def _tokens_conflict(desc_tokens: set[str], source_tokens: set[str]) -> bool:
    """
    Вето: описание заказа и кандидат-маппинг ЯВНО противоречат друг
    другу по валюте или номиналу.

    Если в описании есть валюта (usd) и у кандидата есть валюта (eur),
    и они не пересекаются — это не «слабое совпадение», это ДРУГОЙ
    товар. То же с числами (2 USD vs 20 USD). Такой кандидат
    исключается до скоринга, чтобы он не выиграл за счёт общих слов
    («apple», «gift card» и т.п.).
    """
    desc_cur = desc_tokens & _CURRENCY_TOKENS
    src_cur = source_tokens & _CURRENCY_TOKENS
    if desc_cur and src_cur and desc_cur.isdisjoint(src_cur):
        return True
    desc_num = {
        t for t in desc_tokens if t.isdigit() and len(t) <= _NOMINAL_MAX_LEN
    }
    src_num = {
        t for t in source_tokens if t.isdigit() and len(t) <= _NOMINAL_MAX_LEN
    }
    if desc_num and src_num and desc_num.isdisjoint(src_num):
        return True
    return False


def _is_word_substring(needle: str, haystack: str) -> bool:
    """
    Подстрока с границами слов: «200 robux» НЕ матчится внутри
    «1200 robux» (без паддинга обычный `in` находил такое и мог
    привязать заказ на 1200 робуксов к лоту на 200).
    """
    if not needle or not haystack:
        return False
    return f" {needle} " in f" {haystack} "


def _mapping_match_score(
    *,
    description: str | None,
    mapping: Mapping,
    known_title: str | None,
) -> int:
    desc_norm = _norm_text(description)
    if not desc_norm:
        return 0

    desc_tokens = _text_tokens(description)
    score = 0
    sources_present = 0
    sources_vetoed = 0
    for source, exact_bonus in (
        (mapping.label, 100),
        (known_title, 120),
    ):
        source_norm = _norm_text(source)
        if not source_norm:
            continue
        sources_present += 1
        source_tokens = _text_tokens(source)
        # Вето: конфликт валюты/номинала — источник не участвует в score.
        if _tokens_conflict(desc_tokens, source_tokens):
            sources_vetoed += 1
            continue
        if _is_word_substring(source_norm, desc_norm) or _is_word_substring(
            desc_norm, source_norm
        ):
            score += exact_bonus
        common = desc_tokens & source_tokens
        score += len(common) * 10
        # Совпавшие числа вроде 2/5/10 USD особенно важны для Apple cards.
        score += sum(15 for token in common if token.isdigit())

    # Все доступные источники противоречат описанию → кандидат исключён.
    if sources_present > 0 and sources_vetoed == sources_present:
        return 0
    return score


@dataclass
class AmbiguousMatch:
    """
    Описание заказа похоже сразу на несколько маппингов — автоматический
    выбор запрещён (цена ошибки = покупка не того товара на NS).

    Возвращается из _resolve_mapping вместо Mapping; вызывающий код
    обязан перевести заказ в manual_hold и показать кандидатов оператору.
    """
    candidates: list[str]  # человекочитаемые строки для алерта
    reason: str


async def _resolve_mapping(
    event: FunPayOrderEvent, log, settings: Settings | None = None
) -> Mapping | AmbiguousMatch | None:
    async with session_factory()() as session:
        if event.funpay_lot_id > 0:
            result = await session.execute(
                select(Mapping).where(Mapping.funpay_lot_id == event.funpay_lot_id)
            )
            return result.scalar_one_or_none()

        result = await session.execute(
            select(Mapping).where(Mapping.enabled.is_(True))
        )
        enabled = list(result.scalars().all())
        known_rows = {}
        if enabled:
            known = await session.execute(
                select(KnownLot).where(
                    KnownLot.funpay_lot_id.in_([m.funpay_lot_id for m in enabled])
                )
            )
            known_rows = {row.funpay_lot_id: row for row in known.scalars().all()}

    if not enabled:
        return None

    desc = _norm_text(event.description)
    if desc:
        # Label-стадия: точное вхождение label'а в описание (с границами
        # слов — «200 robux» больше не матчится внутри «1200 robux»).
        # Если подошло НЕСКОЛЬКО label'ов — раньше брался первый по
        # порядку выборки (лотерея), теперь это manual_hold.
        label_matches: list[Mapping] = []
        for mapping in enabled:
            label = _norm_text(mapping.label)
            if label and (
                _is_word_substring(label, desc) or _is_word_substring(desc, label)
            ):
                label_matches.append(mapping)
        if len(label_matches) == 1:
            mapping = label_matches[0]
            log.warning(
                f"FunPay order без lot_id сопоставлен по label: "
                f"order={event.funpay_order_id}, label={mapping.label!r}, "
                f"lot={mapping.funpay_lot_id}"
            )
            return mapping
        if len(label_matches) > 1:
            candidates = [
                f"lot {m.funpay_lot_id} ({m.label})" for m in label_matches[:5]
            ]
            log.error(
                f"FunPay order без lot_id: описание совпало сразу с "
                f"{len(label_matches)} label'ами, не выбираю автоматически. "
                f"candidates={candidates}, description={event.description!r}"
            )
            return AmbiguousMatch(
                candidates=candidates,
                reason=f"описание совпало с {len(label_matches)} label'ами",
            )

    scored: list[tuple[int, Mapping]] = []
    if desc:
        for mapping in enabled:
            known_title = getattr(known_rows.get(mapping.funpay_lot_id), "title", None)
            score = _mapping_match_score(
                description=event.description,
                mapping=mapping,
                known_title=known_title,
            )
            if score > 0:
                scored.append((score, mapping))
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored:
            effective_settings = settings or get_settings()
            min_score = int(getattr(effective_settings, "order_match_min_score", 20))
            min_gap = int(getattr(effective_settings, "order_match_min_gap", 10))
            best_score, best_mapping = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else 0
            # Нужен явный отрыв, чтобы не выбрать случайный Apple-лот при
            # неоднозначном описании. Пороги настраиваются через
            # ORDER_MATCH_MIN_SCORE / ORDER_MATCH_MIN_GAP.
            if best_score >= min_score and best_score >= second_score + min_gap:
                log.warning(
                    f"FunPay order без lot_id сопоставлен по описанию: "
                    f"order={event.funpay_order_id}, lot={best_mapping.funpay_lot_id}, "
                    f"score={best_score}, second={second_score}, "
                    f"description={event.description!r}"
                )
                return best_mapping
            candidates = [
                f"lot {m.funpay_lot_id} ({m.label}) score={score}"
                for score, m in scored[:5]
            ]
            log.error(
                f"FunPay order без lot_id: описание похоже на несколько "
                f"маппингов, не выбираю автоматически. candidates={candidates}, "
                f"description={event.description!r}"
            )
            return AmbiguousMatch(
                candidates=candidates,
                reason=(
                    f"лучший кандидат score={best_score}, второй="
                    f"{second_score} — недостаточный отрыв "
                    f"(нужно ≥{min_score} и отрыв ≥{min_gap})"
                ),
            )

    # ВАЖНО: fallback'а «единственный включённый маппинг» больше НЕТ.
    # Он был опасен: заказ по немаппленному лоту (ручная продажа, напр.
    # CS2) при единственном включённом маппинге покупал на NS совершенно
    # другой товар и отправлял код покупателю.
    log.error(
        f"FunPay order без lot_id и не удалось однозначно выбрать маппинг: "
        f"enabled_mappings={len(enabled)}, description={event.description!r}"
    )
    return None


async def _resolve_chat_id(
    event: FunPayOrderEvent, funpay_client: FunPayClient | None, log
) -> int | None:
    if event.chat_id is not None:
        return event.chat_id
    if funpay_client is None or not event.buyer_username:
        return None

    def _lookup() -> int | None:
        account = funpay_client.account
        get_chat_by_name = getattr(account, "get_chat_by_name", None)
        if callable(get_chat_by_name):
            try:
                chat = get_chat_by_name(event.buyer_username, True)
                chat_id = getattr(chat, "id", None)
                return int(chat_id) if chat_id is not None else None
            except Exception:
                return None
        return None

    try:
        chat_id = await funpay_client._to_thread(_lookup)  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning(
            f"Не смог найти chat_id по buyer_username={event.buyer_username!r}: {exc}"
        )
        return None

    if chat_id is not None:
        log.info(
            f"Восстановил chat_id={chat_id} по buyer_username="
            f"{event.buyer_username!r}"
        )
    return chat_id
