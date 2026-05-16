Документация NS API
REST-API с двухслойной аутентификацией: api_secret (постоянный, выдаётся оператором при регистрации) + session token (короткоживущий, TTL 2 часа, запрашивается через /get_token). Утечка одного без другого бесполезна для атакующего. Ниже - готовый Python-клиент и все рабочие эндпоинты.

Установка
Достаточно одной зависимости:

pip install requestsCopy
Где взять api_secret: оператор выдаёт его один раз при регистрации твоего аккаунта (вместе с user_id и паролем). Сохрани в защищённое хранилище - повторно секрет не отображается ни через какой эндпоинт. Если потерял - оператор пересоздаст аккаунт.

Python-клиент
Один раз сохрани файл как ns_client.py, заполни USER_ID, API_SECRET и BASE_URL - дальше любой эндпоинт вызывается одной строкой.

Headers
header	значение
X-User-Id	числовой user_id
X-Timestamp	unix-секунды (±60 с от сервера)
X-Signature	HMAC-SHA256 подпись (см. формулу)
Формулы подписи
# /get_token (bootstrap, токена ещё нет / no token yet):
string_to_sign = METHOD + "\n" + PATH + "\n" + QUERY + "\n" + TS + "\n" + sha256_hex(BODY)

# Все остальные эндпоинты / every other endpoint:
string_to_sign = METHOD + "\n" + PATH + "\n" + QUERY + "\n" + TS + "\n" + TOKEN + "\n" + sha256_hex(BODY)

# Подпись одинаковая в обоих случаях / signature is the same in both cases:
signature = base64( HMAC-SHA256( base64-decode(api_secret), string_to_sign ) )Copy
Поток
Получи user_id, login, password, api_secret у оператора (один раз)
Вызови POST /get_token → получишь token (TTL 2 часа)
Любой другой запрос: header X-Token + token внутри string_to_sign
За несколько минут до истечения вызови /get_token ещё раз

NS.CLIENT.PY

"""HMAC-signed client for NS API v2 (two-layer auth)."""

import base64
import hashlib
import hmac
import json
import time

import requests

USER_ID = 1234                        # your numeric user id (see Profile in the cabinet)
LOGIN = "your_login"
PASSWORD = "your_password"
API_SECRET = "PASTE-YOUR-BASE64-SECRET"
BASE_URL = "https://api.ns.gifts"

_token = None  # filled in by login()


def _sign(method, path, query, body, ts, token):
    body_hash = hashlib.sha256(body or b"").hexdigest()
    parts = [method.upper(), path, query, ts]
    if token is not None:
        parts.append(token)
    parts.append(body_hash)
    string_to_sign = "\n".join(parts).encode()
    digest = hmac.new(
        base64.b64decode(API_SECRET), string_to_sign, hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()


def login():
    """Bootstrap: exchange login+password for a session token."""
    global _token
    body = json.dumps(
        {"login": LOGIN, "password": PASSWORD}, separators=(",", ":")
    ).encode()
    ts = str(int(time.time()))
    headers = {
        "X-User-Id": str(USER_ID),
        "X-Timestamp": ts,
        # Token=None → bootstrap signing rule (no token slot).
        "X-Signature": _sign("POST", "/api/v2/get_token", "", body, ts, None),
        "Content-Type": "application/json",
    }
    r = requests.post(
        BASE_URL + "/api/v2/get_token",
        headers=headers, data=body, timeout=30,
    )
    r.raise_for_status()
    _token = r.json()["token"]
    return _token


def call(method, path, *, params=None, json_body=None):
    """Signed request. Auto-logs in on first call."""
    if _token is None:
        login()
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    body = (
        b"" if json_body is None
        else json.dumps(json_body, separators=(",", ":")).encode()
    )
    ts = str(int(time.time()))
    headers = {
        "X-User-Id": str(USER_ID),
        "X-Timestamp": ts,
        "X-Token": _token,
        "X-Signature": _sign(method, path, query, body, ts, _token),
        "Content-Type": "application/json",
    }
    url = BASE_URL + path + (f"?{query}" if query else "")
    r = requests.request(method, url, headers=headers, data=body, timeout=30)
    if r.status_code == 401 and _token is not None:
        # Token might have expired - try once more after re-login.
        login()
        headers["X-Token"] = _token
        ts = str(int(time.time()))
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = _sign(method, path, query, body, ts, _token)
        r = requests.request(method, url, headers=headers, data=body, timeout=30)
    r.raise_for_status()
    return r.json()



POST
/api/v2/get_token
Bootstrap-эндпоинт. Подписывается твоим api_secret (без token, потому что его ещё нет), в body передаёшь login+password. В ответ - свежий token с TTL 2 часа. api_secret в ответе НЕТ.

Body
field	type	описание
login	string	твой логин
password	string	пароль
Headers
header	значение
X-User-Id	числовой user_id
X-Timestamp	unix-секунды
X-Signature	подпись по bootstrap-формуле (см. выше)
Можно держать несколько токенов одновременно (разные устройства / процессы). Каждый /get_token выдаёт дополнительный токен - старые не отзываются, истекают по TTL.

Python
from ns_client import login

token = login()
print("token:", token)  # use it implicitly via ns_client.call(...)

Response
{
  "user_id": 1234,
  "token": "Hg7K-rT3kY...43-char-base64url...",
  "expires_in": 7200
}

GET
/api/v2/stock
Каталог категорий и сервисов: твои цены, остатки и - для каждой категории - массив fields со схемой того, что класть в fields при create_order для услуг этой категории.

Python

from ns_client import call

stock = call("GET", "/api/v2/stock")
for cat in stock["categories"]:
    print(f"--- {cat['category_name']} (id={cat['category_id']}) ---")
    for svc in cat["services"]:
        print(
            f"  {svc['service_id']:<6} "
            f"{svc['service_name']:<40} "
            f"{svc['price']:>8.4f} {svc['currency']}  "
            f"in_stock={svc['in_stock']}"
        )



Response

{
  "categories": [
    {
      "category_name": "Apple Gift Card USA",
      "category_id": 17,
      "services": [
        {
          "service_id": 20,
          "service_name": "Apple Gift Card | USA | 2 USD",
          "price": 1.928,
          "currency": "USD",
          "in_stock": 73
        }
      ],
      "fields": [
        {
          "key": "quantity",
          "type": "int",
          "name": "Quantity",
          "required": true,
          "min": 1,
          "max": 100,
          "step": 1
        }
      ]
    }
  ]
}


POST
/api/v2/create_order
Создаёт заказ. Оплата отдельным шагом через pay_order. custom_id придумываешь сам - это UUID4, он же потом используется для запроса статуса.

Body
field	type	описание
service_id	int	из /stock
custom_id	string	UUID4, ты сам генерируешь
fields	array	список {key, value} по схеме категории из /stock

Python - code purchase (service_id 449)
import uuid
from ns_client import call

# code_purchase template - fields: [quantity (int 1..500)]
resp = call("POST", "/api/v2/create_order", json_body={
    "service_id": 449,
    "custom_id": str(uuid.uuid4()),
    "fields": [
        {"key": "quantity", "value": 1},
    ],
})
print(resp)


Python - Steam top-up (service_id 1)

# steam_topup template - fields:
#   account (string, regex ^[a-zA-Z0-9_]{3,32}$)
#   amount  (float USD, 0.13 .. 500)
resp = call("POST", "/api/v2/create_order", json_body={
    "service_id": 1,
    "custom_id": str(uuid.uuid4()),
    "fields": [
        {"key": "account", "value": "steam_login"},
        {"key": "amount",  "value": 10.0},
    ],
})
print(resp)

Python - Steam Gift (service_id 394)
# steam_gift template - fields:
#   region          (enum: ru|kz|ua|cis|cn)
#   sub_id          (int - get from /steam_gift/get_apps)
#   friendLink      (string, ^https://s\.team/p/[A-Za-z0-9-]+/[A-Za-z0-9]+$)
#   giftName        (string, required)
#   giftDescription (string, optional)
resp = call("POST", "/api/v2/create_order", json_body={
    "service_id": 394,
    "custom_id": str(uuid.uuid4()),
    "fields": [
        {"key": "region",          "value": "ru"},
        {"key": "sub_id",          "value": 12345},
        {"key": "friendLink",      "value": "https://s.team/p/abc-defg/12345678"},
        {"key": "giftName",        "value": "Подарок"},
        {"key": "giftDescription", "value": ""},
    ],
})
print(resp)

Response (общий для всех типов / shared across types)
{
  "custom_id": "a4cee2fe-ce8c-448b-bf2c-...",
  "total_to_pay": "3.8560",
  "status": "created"
}


POST
/api/v2/pay_order
Подтверждает оплату созданного заказа. Списывает баланс, исполняет доставку, возвращает результат. Идемпотентность не реплеит: повторный вызов на тот же custom_id вернёт 409 - сохрани первый ответ.

Body
field	type	описание
custom_id	string	тот же что в create_order
totp_code	string?	6-значный код Authenticator (если включена 2FA на покупки)
status="in_progress" = асинхронная доставка (Steam Gift). Проверяй статус через order_info каждые несколько секунд до перехода в completed или refunded.

Python
from ns_client import call

resp = call("POST", "/api/v2/pay_order", json_body={
    "custom_id": "a4cee2fe-ce8c-448b-bf2c-...",
})
# status: completed | refunded | in_progress | insufficient
print("status:", resp["status"])
print("balance:", resp["balance"])
print("pins:", resp.get("pins"))

Response

{
  "custom_id": "a4cee2fe-ce8c-448b-bf2c-...",
  "status": "completed",
  "balance": "127.4153",
  "pins": ["X4PT-9QL4-E3NR", "Y8QP-5A4C-NN2B"],
  "note": null
}

GET
/api/v2/order_info/{custom_id}
Полная информация о заказе: статус, доставленные пины, сумма, дата.

Коды статуса
status	значение
0	создан, но не оплачен
10	в процессе (Steam Gift)
2	завершён, доставлен
7	возврат
5	отменён (15 мин без оплаты)

Python
from ns_client import call

custom_id = "a4cee2fe-ce8c-448b-bf2c-..."
info = call("GET", f"/api/v2/order_info/{custom_id}")
print(info["status_message"])  # "Completed" / "In Progress" / ...
print(info.get("pins"))        # delivered codes if any

Response

{
  "custom_id": "a4cee2fe-ce8c-448b-bf2c-...",
  "status": 2,
  "status_message": "Completed",
  "product": "Apple Gift Card | USA | 2 USD",
  "quantity": 2.0,
  "total_price": 3.856,
  "date": "2026-05-04T22:55:36",
  "pins": ["X4PT-9QL4-E3NR", "Y8QP-5A4C-NN2B"],
  "data": null
}

POST
/api/v2/exchange_rate
Курсы валют для конкретного сервиса. Сейчас доступен только service_id=1 (Steam пополнение): возвращает RUB / KZT / UAH за 1 USD.

Body
field	type	описание
service_id	int	пока только 1

Python
from ns_client import call

rates = call("POST", "/api/v2/exchange_rate", json_body={"service_id": 1})
print(rates["rates"])  # {"rub": 95.42, "kzt": 480.1, "uah": 41.3}

Response
{
  "service_id": 1,
  "date": "2026-05-04T22:55:00",
  "rates": {
    "rub": 95.42,
    "kzt": 480.10,
    "uah": 41.30
  }
}

GET
/api/v2/check_balance
Текущий баланс кабинета в USD (4 знака после запятой).

Никаких параметров - баланс возвращается для пользователя из X-User-Id.

Python
from ns_client import call

balance = call("GET", "/api/v2/check_balance")
print(balance["balance"])  # "305.2403"

Response
{
  "balance": "305.2403"
}

GET
/api/v2/steam_gift/get_apps
Каталог Steam-игр доступных для подарка: app_id, имя, доступные регионы, цены, sub_id'ы (нужны для create_order на steam_gift).

Python
from ns_client import call

apps = call("GET", "/api/v2/steam_gift/get_apps")
for a in apps["apps"][:5]:
    print(a["app_id"], "-", a["name"])

Response

{
  "apps": [
    {
      "app_id": 730,
      "name": "Counter-Strike 2",
      "data_json": { "subs": [] }
    }
  ]
}

POST
/api/v2/steam/check_user
Проверяет существование Steam-логина. Возвращает true если логин валиден.

Body
field	type	описание
steam_id	string	Steam-логин юзера (account_number)

Python
from ns_client import call

resp = call("POST", "/api/v2/steam/check_user",
            json_body={"steam_id": "ecldu72762"})
print("valid:", resp["accountStatus"])

Response
{ "accountStatus": true }


Коды ошибок
HTTP	смысл
400	плохие параметры
401	подпись/таймстамп/nonce невалидны, или нет 2FA-кода
403	IP не в whitelist; нет нужного права
404	заказ/сервис не найден
409	конфликт (повтор оплаты, дубликат заказа, …)
428	требуется TOTP-код
500	внутренняя ошибка - пиши в саппорт
Nonce / replay: каждая подпись принимается только один раз (Redis-хранилище 120 сек). Не отправляй повторно - генерируй свежий X-Timestamp на каждый запрос.