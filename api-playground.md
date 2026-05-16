API Playground
Стенд для V2 API. Вызывает те же бизнес-функции, что и реальный /api/v2/* - но под твоей текущей сессией кабинета. Полная документация: /api-docs.

Чем playground отличается от реального API:
Здесь авторизация через cookie-сессию кабинета - HMAC-подпись и токен из /get_token не нужны.
IP-whitelist не проверяется. В боевой интеграции с /api/v2/* запросы с не-белого IP отвергаются с 403 - обязательно попроси поддержку добавить IP твоего сервера в твой список.
/pay_order здесь отключён - реальную оплату через playground провести нельзя.
GET
/api/v2/stock
Stock - что есть в наличии
Список категорий и услуг с остатками и итоговой ценой. Каждая категория несёт массив fields - это и есть схема, по которой нужно собирать массив fields в /create_order для услуг этой категории.

▶ Run
POST
/api/v2/exchange_rate
Exchange rate - курсы валют
Курсы валют, по которым считается итоговая цена в USD для конкретного service_id.

Body (JSON)
{
  "service_id": 1
}
▶ Run
POST
/api/v2/create_order
Create order - покупка кода (service_id 449)
Покупка готового кода (gift card / digital code). Ничего не списывает - это только бронь. В реальной интеграции дальше вызывается /pay_order (тут отключён).

custom_id генерируется автоматически. quantity = сколько кодов хочешь. Кнопка ↻ ниже - обновить UUID для следующего теста.

Body (JSON)
{
  "service_id": 449,
  "custom_id": "0cb6f977-0f69-4875-a337-a3cf8f3cd118",
  "fields": [
    { "key": "quantity", "value": 1 }
  ]
}
▶ Run
↻ New UUID
POST
/api/v2/create_order
Create order - пополнение Steam (service_id 1)
Пополнение Steam-аккаунта. Шаблон полей берётся из ns_field_templates → steam_topup (pricing_mode by_amount).

account - Steam-логин или email получателя (плюс символы _ . + -). amount - сумма в USD (0.13 - 500). Никакого region - Steam сам конвертирует.

Body (JSON)
{
  "service_id": 1,
  "custom_id": "e16f99cf-5f68-490c-9150-b0db59118c39",
  "fields": [
    { "key": "account", "value": "steam_login" },
    { "key": "amount", "value": 1.00 }
  ]
}
▶ Run
↻ New UUID
POST
/api/v2/create_order
Create order - Steam Gift (service_id 394)
Подарок игры. Шаблон полей из ns_field_templates → steam_gift (pricing_mode external_steam_gift). Перед запуском возьми sub_id из /steam_gift/get_apps.

region - один из ru/kz/ua/cis/cn (нижний регистр). sub_id - числовой id подписки игры (берётся из /steam_gift/get_apps). friendLink - короткий invite https://s.team/p/<hash>/<friend_code>. giftName обязателен, giftDescription нет.

Body (JSON)
{
  "service_id": 394,
  "custom_id": "ba3bffae-9cdc-4210-8e41-13560f0aa019",
  "fields": [
    { "key": "region", "value": "ru" },
    { "key": "sub_id", "value": 0 },
    { "key": "friendLink", "value": "https://s.team/p/abc-defg/12345678" },
    { "key": "giftName", "value": "Подарок" },
    { "key": "giftDescription", "value": "" }
  ]
}
▶ Run
↻ New UUID
GET
/api/v2/check_balance
Check balance - твой баланс
Текущий баланс кабинета (USD, до 4 знаков).

▶ Run
GET
/api/v2/order_info/{custom_id}
Order info - посмотреть заказ
Текущий статус заказа + (если он завершён) выданные pins или данные доставки. В реальном API это GET с custom_id в URL - здесь для удобства передаётся в теле.

Body (JSON)
{
  "custom_id": "playground-test-1"
}
▶ Run
GET
/api/v2/steam_gift/get_apps
Steam Gift - список игр
Каталог игр, которые можно купить через Steam Gift.

▶ Run
POST
/api/v2/steam/check_user
Steam - проверка получателя
Перед созданием Steam-Gift заказа - проверяет что аккаунт получателя существует.

Body (JSON)
{
  "steam_id": "your_friend_login"
}
▶ Run