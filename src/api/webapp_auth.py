"""
Sprint 6 — Telegram WebApp (Mini App) аутентификация.

Telegram Mini App при открытии передаёт фронту `window.Telegram.WebApp.initData` —
строку формата URL query string с параметрами (user, auth_date, query_id, и т.д.)
плюс `hash` — HMAC-SHA256 от остальных полей.

Поток валидации (документация Telegram, разделы Mini Apps → Authorization):

  1. Из initData выделяем hash (= ожидаемая подпись).
  2. Остальные ключи сортируем по алфавиту и собираем строку:
       data_check_string = '\n'.join(f"{key}={value}" for ...)
  3. Считаем secret_key = HMAC_SHA256("WebAppData", bot_token).digest()
  4. Считаем expected_hash = HMAC_SHA256(secret_key, data_check_string).hexdigest()
  5. Сравниваем expected_hash == provided_hash (secure compare).
  6. Дополнительно проверяем `auth_date` — не старше 24 часов (anti-replay).

Если проверка прошла — возвращаем typed-объект с user/auth_date.
Иначе бросаем WebAppAuthError, FastAPI превратит в 401.

Безопасность:
  * `secrets.compare_digest` — против timing attacks.
  * `auth_date` против повторного использования старого initData.
  * Bot token хранится в settings; никогда не возвращается клиенту.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

# Максимальный возраст initData — после которого данные считаются устаревшими.
# Telegram рекомендует 24 часа, у нас по умолчанию 12 часов для большей
# безопасности (юзер всё равно держит Mini App открытым обычно меньше).
DEFAULT_MAX_AGE_SECONDS = 12 * 60 * 60


class WebAppAuthError(Exception):
    """Любая ошибка валидации initData. FastAPI рендерит в 401 Unauthorized."""


@dataclass(frozen=True)
class WebAppUser:
    """
    Доверенная информация о пользователе из initData.

    Поля: согласно Telegram docs — id обязательно, остальные опциональны.
    `language_code` важен для будущей i18n.
    """
    id: int
    is_bot: bool = False
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""
    is_premium: bool = False
    allows_write_to_pm: bool = True
    photo_url: str = ""


@dataclass(frozen=True)
class WebAppInitData:
    """
    Полностью провалидированный initData. Содержит юзера и auth_date.

    Если когда-нибудь понадобятся chat/receiver/start_param — добавляются
    сюда же без breaking change.
    """
    user: WebAppUser
    auth_date: int
    query_id: str = ""
    start_param: str = ""
    chat_instance: str = ""
    chat_type: str = ""


def _compute_secret_key(bot_token: str) -> bytes:
    """
    secret_key = HMAC_SHA256("WebAppData", bot_token).digest()

    Это первичный «корень доверия». bot_token должен оставаться приватным
    на сервере — он же расшифровывает initData от Telegram.
    """
    return hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()


def _parse_user(raw: str) -> WebAppUser:
    """
    Парсит JSON-строку user в типизированный WebAppUser.

    Telegram присылает user как URL-encoded JSON; parse_qsl уже декодирует
    URL-encoding, нам остаётся json.loads.

    Никаких лишних полей не игнорируем (forward-compat), но конструируем
    WebAppUser только из известных полей.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise WebAppAuthError(f"user is not valid JSON: {exc}")
    if not isinstance(obj, dict):
        raise WebAppAuthError("user field is not an object")
    if "id" not in obj:
        raise WebAppAuthError("user.id missing")
    try:
        user_id = int(obj["id"])
    except (ValueError, TypeError):
        raise WebAppAuthError("user.id is not int")
    return WebAppUser(
        id=user_id,
        is_bot=bool(obj.get("is_bot", False)),
        first_name=str(obj.get("first_name", "")),
        last_name=str(obj.get("last_name", "")),
        username=str(obj.get("username", "")),
        language_code=str(obj.get("language_code", "")),
        is_premium=bool(obj.get("is_premium", False)),
        allows_write_to_pm=bool(obj.get("allows_write_to_pm", True)),
        photo_url=str(obj.get("photo_url", "")),
    )


def verify_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> WebAppInitData:
    """
    Главная функция модуля — валидирует initData и возвращает доверенный объект.

    Бросает WebAppAuthError при любой проблеме (битый формат, истёк, не сошёлся
    hash, нет user, и т.д.). Никогда не возвращает None.

    Параметры:
      * init_data: строка из `window.Telegram.WebApp.initData`
      * bot_token: токен бота, к которому привязан Mini App
                   (это shop_telegram_bot_token, не bridge bot)
      * max_age_seconds: устаревание initData; 12h по умолчанию
      * now: для тестов (фиксация времени); в проде — time.time()
    """
    if not init_data:
        raise WebAppAuthError("init_data empty")
    if not bot_token:
        raise WebAppAuthError("bot_token not configured")

    # Парсинг URL-encoded query string. keep_blank_values=True чтобы
    # пустые поля (например username="") не отвалились.
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise WebAppAuthError("init_data has no parameters")

    # Извлекаем hash отдельно — он не входит в data_check_string
    received_hash: str | None = None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            fields[key] = value
    if received_hash is None:
        raise WebAppAuthError("hash missing in init_data")

    # data_check_string — алфавитно отсортированные key=value\n...
    data_check_string = "\n".join(
        f"{k}={fields[k]}" for k in sorted(fields.keys())
    )

    secret_key = _compute_secret_key(bot_token)
    expected_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    # secure compare — против timing attacks
    if not hmac.compare_digest(expected_hash, received_hash):
        raise WebAppAuthError("hash mismatch (init_data tampered or wrong bot_token)")

    # auth_date — UNIX timestamp когда initData был выдан Telegram'ом
    auth_date_raw = fields.get("auth_date")
    if not auth_date_raw:
        raise WebAppAuthError("auth_date missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        raise WebAppAuthError("auth_date is not int")

    current_ts = now if now is not None else time.time()
    age = int(current_ts) - auth_date
    if age > max_age_seconds:
        raise WebAppAuthError(
            f"init_data expired (age={age}s > max={max_age_seconds}s)"
        )
    if age < -300:
        # Толерантность 5 минут к расхождению часов сервера и устройства
        raise WebAppAuthError(f"auth_date is in the future ({age}s)")

    # User — обязательный
    user_raw = fields.get("user")
    if not user_raw:
        raise WebAppAuthError("user missing in init_data")
    user = _parse_user(user_raw)

    return WebAppInitData(
        user=user,
        auth_date=auth_date,
        query_id=fields.get("query_id", ""),
        start_param=fields.get("start_param", ""),
        chat_instance=fields.get("chat_instance", ""),
        chat_type=fields.get("chat_type", ""),
    )
