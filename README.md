# NS.gifts ⇄ FunPay bridge

Перепродажа цифровых товаров с оптового поставщика `ns.gifts` на FunPay.
Слежение за каталогом, автоматическая покупка после продажи, доставка кодов
в чат и присмотр через Telegram.

## Что делает

- Раз в `SYNC_INTERVAL_SECONDS` сверяет каталог `ns.gifts` с твоими лотами на
  FunPay: пересчитывает цену с наценкой и текущим курсом, подгоняет остаток,
  деактивирует лот при отсутствии товара.
- Каждые `NEW_LOTS_CHECK_INTERVAL_SECONDS` ищет на твоём FunPay-аккаунте лоты,
  у которых ещё нет маппинга, и присылает в Telegram карточку с кнопкой
  «🎯 Выбрать целью» — два клика и лот привязан к NS-сервису.
- Слушает FunPay-чат. Когда приходит заказ — создаёт заказ на `ns.gifts`,
  оплачивает его, забирает коды, отправляет покупателю. Если код получен,
  но отправка в чат FunPay сорвалась — заказ остаётся в статусе `pins_ready`,
  и при следующем заходе бот доставит код повторно без новой покупки.
- Отвечает на сообщения в чате: приветствие до покупки, инструкция, реакция
  на команду помощи.
- Сообщает владельцу в Telegram о новых заказах, ошибках, низком балансе,
  просьбах о помощи и появлении новых FunPay-лотов.

## Сквозной workflow

1. Создаёшь лот на funpay.com. Через минуту-три бот пишет в Telegram:
   «🆕 Новый лот ID 12345 …» с кнопками «🎯 Выбрать целью» и «🔬 Inspect».
2. Нажимаешь «🎯 Выбрать целью» → лот становится «целевым».
3. Открываешь **🗂 Каталог NS** или пишешь `/ns_search apple usa 5`.
4. У нужной услуги жмёшь «✅ Замапить» — маппинг создан с глобальной
   наценкой (`MARKUP_PERCENT`, по умолчанию 6%).
5. Через `SYNC_INTERVAL_SECONDS` (по умолчанию 30 с) бот сам выставит цену
   и сток на FunPay; дальше обновляет цикл за циклом, пока маппинг включён.
6. Покупка идёт автоматически: FunPay-заказ → NS create → NS pay → коды
   приходят клиенту. Все шаги пишутся в Telegram и в БД.

## Стек

- Python 3.11+
- `httpx`, `pydantic-settings`, `SQLAlchemy 2 async + aiosqlite`, `APScheduler`,
  `loguru`, `pyotp`
- `aiogram 3.x` — Telegram-бот
- `FastAPI + uvicorn` — отдельный Web API для будущей админки
- `FunPayAPI` (неофициальная) — взаимодействие с FunPay через cookies

## Структура

```
.
├── data/                 # БД SQLite, маппинги (не в git)
├── deploy/               # systemd unit, bootstrap, update.sh
├── logs/                 # ротация логов
├── src/
│   ├── config.py         # настройки из .env
│   ├── main.py           # entry point: один asyncio-процесс
│   ├── ns/               # клиент ns.gifts (HMAC v2)
│   ├── funpay/           # клиент FunPay + watcher (chat/orders)
│   ├── db/               # модели и репозиторий
│   ├── mapping/          # связки funpay_lot_id ⇄ ns_service_id, цены
│   ├── sync/             # синхронизатор каталога, курс
│   ├── orders/           # пайплайн заказов
│   ├── services/         # application services для Telegram/Web API
│   ├── api/              # FastAPI backend для будущего сайта
│   ├── chat/             # шаблоны, рабочие часы, реакции на сообщения
│   ├── alerts/           # Telegram: нотификации и интерактивный бот
│   └── tools/            # CLI-утилиты (проверки, импорт, диагностика)
└── tests/                # pytest
```

## Локальная установка

```powershell
cd D:\money
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
# заполнить .env
```

## Конфигурация

`.env` лежит в корне и не попадает в git. Опорные группы переменных:

- `NS_*` — поставщик (login, password, api secret, опциональный TOTP).
- `FUNPAY_*` — cookies `golden_key` и `phpsessid`, твой user_id.
- `MARKUP_PERCENT`, `FUNPAY_CURRENCY`, `USD_RUB_RATE_MODE`, `USD_RUB_RATE` —
  ценообразование.
- `TELEGRAM_*` — bot token, chat id, прокси при необходимости.
- `WEB_API_*` — отдельный защищённый API для будущего сайта.
- `CHAT_*`, `WORK_HOURS_*` — поведение чата с покупателем.
- `ENABLE_REAL_ACTIONS` — главный предохранитель. Пока `false`, все «опасные»
  действия (оплата на NS, изменения на FunPay) идут в dry-run.

Подробности — в `.env.example`.

## Запуск локально

```powershell
# проверки
python -m src.tools.check_ns
python -m src.tools.check_funpay
python -m src.tools.check_fx
python -m src.tools.check_telegram

# определить TELEGRAM_CHAT_ID, если ещё не известен
python -m src.tools.discover_chat_id

# полный процесс (sync + watcher + Telegram-бот)
python -m src.main

# отдельный Web API для будущего сайта
python -m src.api.main

# тесты
python -m pytest tests/ -q
```

## CLI-утилиты

| Команда | Назначение |
| --- | --- |
| `src.tools.check_ns` | Проверка авторизации в `ns.gifts`, баланс, фрагмент каталога |
| `src.tools.check_funpay` | Авторизация FunPay, проверка cookies, диагностика API |
| `src.tools.check_fx` | Курс USD→RUB (auto/manual) |
| `src.tools.check_telegram` | Тестовое сообщение в Telegram |
| `src.tools.check_web_api` | Smoke-проверка локального Web API |
| `src.tools.discover_chat_id` | Узнать свой chat_id по первому сообщению боту |
| `src.tools.list_funpay_lots` | Список твоих лотов на FunPay (для маппингов) |
| `src.tools.import_mappings <csv>` | Импорт маппингов из CSV в БД |
| `src.tools.dry_run_sync` | Один прогон синхронизатора без записи на FunPay |
| `src.tools.test_order` | Эмулировать FunPay-заказ и прогнать через pipeline |

## Telegram-бот

Управление и наблюдение — через личного бота. Большинство команд доступны только
владельцу (`TELEGRAM_CHAT_ID`); `/ping`, `/version`, `/whoami`, `/start` — открытые
пробники.

### Главное меню

`/menu` (или `/start`) открывает inline-меню:

```
📊 Статус        💰 Балансы
🛒 Лоты FunPay   🗺 Маппинги
🗂 Каталог NS    🔍 Поиск NS
🔄 Синхронизация 📦 Заказы
🔌 FunPay reconnect ❓ Помощь
```

Длинные списки (NS-услуги, лоты, маппинги, заказы) показываются по 10 строк
с пагинацией кнопками ◀ / X из Y / ▶. Сессия пагинации живёт час.

### Воркфлоу «замаппить лот за два клика»

1. Открой раздел **🛒 Лоты FunPay** и нажми «🎯 Выбрать #ID» на нужном лоте.
2. Перейди в **🗂 Каталог NS** или вызови `/ns_search apple usa 5`.
3. На карточке услуги нажми «✅ Замапить» — маппинг сохранён.

В разделе **🗺 Маппинги** напротив каждой строки — кнопка «⏸/▶» (вкл/выкл) и
«🗑» (удалить). Для лотов есть «📊 Расчёт» (быстрый /calc) и «🔬» (inspect полей).

### Команды

```
/menu               главное меню с кнопками
/status             состояние, последний sync, балансы
/balance            балансы NS + FunPay
/orders             последние 50 заказов с пагинацией
/sync               запустить sync прямо сейчас
/funpay_reconnect   переподключить FunPay

/ns_search <слова>  поиск по каталогу ns.gifts (с пагинацией)
/ns_cats            список категорий (drill-down кнопками)

/lots               мои лоты на FunPay (с кнопками действий)
/mappings           текущие маппинги (с кнопками вкл/выкл/удалить)
/map <funpay_lot_id> <ns_service_id> [markup%] [label]
/unmap <funpay_lot_id>
/calc <funpay_lot_id>     расчёт цены продавцу/клиенту по маппингу
/inspect_lot <funpay_lot_id>   LotFields для отладки
/setmarkup <lot_id> <percent|default>  индивидуальная наценка для маппинга
/reset_markups       обнулить все индивидуальные наценки (использовать global)
/clear_target        сбросить выбранный целевой лот

/settings            показать активные runtime-настройки
/setdefault markup <%|default>    глобальная наценка без рестарта
/setdefault premium <%|default>   премия к курсу USD
/setdefault stockcap <N|default>  лимит остатков на FunPay

/ping               проверка long-polling (отвечает всем)
/version            версия + chat_id + статус владения
/whoami             свой chat_id
/help               подсказка
```

## Развёртывание на VPS

См. `deploy/README.md`. Коротко:

```bash
bash /opt/funpay-ns-bot/deploy/update.sh        # обновить код
systemctl daemon-reload
systemctl enable --now funpay-ns-bot
journalctl -u funpay-ns-bot -f
```

Web API запускается отдельным сервисом и по умолчанию слушает только
`127.0.0.1:8080`; наружу его нужно выпускать только через reverse proxy с TLS:

```bash
systemctl enable --now funpay-ns-api
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.check_web_api
```

Админка сайта уже встроена в этот же сервис: открой `/` на Web API, вставь
`WEB_API_TOKEN` и увидишь Dashboard, Orders, Problems, Mappings и Profit.
Если прямой SSH/туннель к VPS режется оператором, подними Cloudflare Tunnel:

```bash
CLOUDFLARE_TUNNEL_TOKEN='<token_из_Cloudflare>' \
  bash /opt/funpay-ns-bot/deploy/install_cloudflare_tunnel.sh
```

В Cloudflare Public Hostname service должен указывать на
`http://127.0.0.1:8080`.

## Безопасность

- Все секреты — в `.env`. Файл не попадает в git и не должен передаваться по
  открытым каналам. На сервере `chmod 600 .env`.
- `ENABLE_REAL_ACTIONS=false` блокирует любые реальные платежи и изменения
  лотов. Переключать только тогда, когда всё проверено в dry-run.
- 2FA на `ns.gifts` (TOTP) поддержано: положи секрет в `NS_TOTP_SECRET`.
- Если потерял `NS_API_SECRET` — оператор `ns.gifts` пересоздаёт аккаунт,
  старый секрет повторно не показывается.
- При подозрении на утечку FunPay-сессии — выйти из всех устройств на
  funpay.com, получить новые `golden_key` и `PHPSESSID`, обновить `.env`.

## Лицензия

Использование на свой страх и риск. Это инструмент для конкретного оператора,
а не публичный продукт. FunPayAPI — неофициальная библиотека; её поведение
может меняться, поэтому код устойчив к разным версиям через runtime-проверки.
