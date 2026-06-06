"""
Подписанные session-токены для сайта neurodrop.ru (cookie-сессии).

Зачем своё, а не JWT-библиотека: токен предельно простой (uid + срок),
а тащить зависимость ради HS256 незачем — `hmac`/`hashlib` из stdlib
делают ровно то же. Формат:

    <base64url(payload_json)>.<base64url(hmac_sha256(payload))>

payload = {"uid": <ShopUser.id>, "tid": <telegram_user_id>, "exp": <unix>}

Безопасность:
  * HMAC-SHA256 с серверным секретом — клиент не может подделать uid;
  * `exp` проверяется при верификации (истёкший токен отвергается);
  * `hmac.compare_digest` против timing-атак;
  * секрет берётся из settings.web_session_secret, а если он не задан —
    детерминированно выводится из bot-токена shop-бота (sha256), чтобы
    фича работала «из коробки», но секрет всё равно не покидал сервер.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from src.config import Settings


class SessionError(Exception):
    """Любая проблема с session-токеном: битый формат, подпись, истёк."""


@dataclass(frozen=True)
class SessionClaims:
    user_id: int
    telegram_user_id: int
    exp: int


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * ((-len(data)) % 4)
    return base64.urlsafe_b64decode(data + pad)


def session_secret(settings: Settings) -> bytes:
    """
    Возвращает байтовый секрет для подписи. Приоритет:
      1. settings.web_session_secret (если задан явно);
      2. sha256(shop_telegram_bot_token) — детерминированный fallback.
    Бросает SessionError, если нет ни того, ни другого (нечем подписывать).
    """
    explicit = settings.web_session_secret
    if explicit is not None:
        return explicit.get_secret_value().encode("utf-8")
    bot = settings.shop_telegram_bot_token
    if bot is not None:
        return hashlib.sha256(
            ("nd-session:" + bot.get_secret_value()).encode("utf-8")
        ).digest()
    raise SessionError("no web_session_secret and no shop_telegram_bot_token")


def issue_session_token(
    *,
    user_id: int,
    telegram_user_id: int,
    settings: Settings,
    now: float | None = None,
) -> str:
    """Создаёт подписанный токен со сроком settings.site_session_ttl_seconds."""
    issued = int(now if now is not None else time.time())
    exp = issued + int(settings.site_session_ttl_seconds)
    payload = {"uid": int(user_id), "tid": int(telegram_user_id), "exp": exp}
    payload_b = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signing_input = _b64url_encode(payload_b)
    sig = hmac.new(
        session_secret(settings), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


def verify_session_token(
    token: str,
    *,
    settings: Settings,
    now: float | None = None,
) -> SessionClaims:
    """
    Проверяет подпись и срок. Возвращает SessionClaims или бросает SessionError.
    """
    if not token or "." not in token:
        raise SessionError("malformed token")
    signing_input, _, sig_part = token.partition(".")
    expected = hmac.new(
        session_secret(settings), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(sig_part)
    except Exception as exc:  # noqa: BLE001
        raise SessionError(f"bad signature encoding: {exc}") from exc
    if not hmac.compare_digest(expected, provided):
        raise SessionError("signature mismatch")
    try:
        payload = json.loads(_b64url_decode(signing_input))
    except Exception as exc:  # noqa: BLE001
        raise SessionError(f"bad payload: {exc}") from exc
    try:
        uid = int(payload["uid"])
        tid = int(payload["tid"])
        exp = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionError(f"missing claims: {exc}") from exc
    current = int(now if now is not None else time.time())
    if current >= exp:
        raise SessionError("token expired")
    return SessionClaims(user_id=uid, telegram_user_id=tid, exp=exp)
