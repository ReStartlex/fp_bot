"""
Верификация Telegram **Login Widget** (вход на сайте через Telegram).

⚠ Это НЕ то же самое, что Mini App initData (см. webapp_auth.py):

  Mini App:      secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
  Login Widget:  secret = SHA256(bot_token)              ← здесь

Алгоритм (Telegram docs → «Checking authorization»):
  1. data — словарь полей виджета: id, first_name, last_name, username,
     photo_url, auth_date, hash.
  2. data_check_string = "\n".join(sorted "key=value" по ключу, кроме hash).
  3. secret_key = SHA256(bot_token).
  4. expected = HMAC_SHA256(secret_key, data_check_string).hexdigest().
  5. expected == data["hash"]  (compare_digest).
  6. auth_date не старше max_age (anti-replay).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any, Mapping


class LoginAuthError(Exception):
    """Невалидные данные Login Widget. FastAPI рендерит в 401."""


@dataclass(frozen=True)
class LoginWidgetUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    photo_url: str = ""
    auth_date: int = 0


def verify_login_widget(
    data: Mapping[str, Any],
    *,
    bot_token: str,
    max_age_seconds: int,
    now: float | None = None,
) -> LoginWidgetUser:
    """Проверяет подпись и свежесть. Возвращает LoginWidgetUser или бросает."""
    if not data:
        raise LoginAuthError("empty login data")
    if not bot_token:
        raise LoginAuthError("bot_token not configured")

    received_hash = data.get("hash")
    if not received_hash:
        raise LoginAuthError("hash missing")

    # data_check_string: все поля кроме hash, отсортированы по ключу.
    # Значения приводим к str ровно так, как их прислал Telegram.
    pairs = {
        str(k): ("" if v is None else str(v))
        for k, v in data.items()
        if k != "hash"
    }
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, str(received_hash)):
        raise LoginAuthError("hash mismatch (tampered or wrong bot_token)")

    auth_date_raw = pairs.get("auth_date")
    if not auth_date_raw:
        raise LoginAuthError("auth_date missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        raise LoginAuthError("auth_date is not int")
    current = int(now if now is not None else time.time())
    age = current - auth_date
    if age > max_age_seconds:
        raise LoginAuthError(f"login expired (age={age}s > {max_age_seconds}s)")
    if age < -300:
        raise LoginAuthError(f"auth_date in the future ({age}s)")

    user_id_raw = pairs.get("id")
    if not user_id_raw:
        raise LoginAuthError("id missing")
    try:
        user_id = int(user_id_raw)
    except ValueError:
        raise LoginAuthError("id is not int")

    return LoginWidgetUser(
        id=user_id,
        first_name=pairs.get("first_name", ""),
        last_name=pairs.get("last_name", ""),
        username=pairs.get("username", ""),
        photo_url=pairs.get("photo_url", ""),
        auth_date=auth_date,
    )
