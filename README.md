# NS.gifts ⇄ FunPay Bridge

Бот для автоматизации перепродажи цифровых товаров с **ns.gifts** (опт) на **FunPay** (розница).

## Что делает

- Синхронизирует каталог ns.gifts → FunPay (цены, остатки, активация лотов) с твоей наценкой.
- При покупке на FunPay автоматически покупает товар на ns.gifts и отправляет код покупателю в чат.
- Отвечает покупателям красивыми шаблонами, просит отзыв после продажи.
- Шлёт алерты в Telegram (через прокси для РФ).

---

## Текущая стадия

**F0–F6 + main entrypoint готовы. Бот цельный, можно запускать.**

- **F0**: Конфиг (`src/config.py`), NS-клиент с HMAC + auto-refresh + ретраями (`src/ns/`), `check_ns` CLI.
- **F1**: FunPay-клиент-обёртка (`src/funpay/`), CLI: `check_funpay`, `list_funpay_lots`.
- **F2**: SQLite (SQLAlchemy + aiosqlite) — модели `Mapping`, `Order`, `FxRate`, `SyncRun`.
  CSV-импорт маппингов. Курс USD/RUB с ЦБ РФ с кэшем. Sync engine (dry-run / real).
- **F3**: order processor: FunPay → NS create → pay → доставка кодов в чат → запись в БД → Telegram-отчёт.
  Идемпотентность. Dry-run пока `ENABLE_REAL_ACTIONS=false`.
- **F4**: шаблоны сообщений покупателю (`src/chat/templates.py`, ru/en).
- **F5**: FunPay watcher (`src/funpay/watcher.py`) — слушает события Runner'а в потоке,
  отдаёт нормализованные события в asyncio.
- **F6**: Telegram — нотификатор (алерты) + интерактивный бот с командами
  `/status`, `/balance`, `/orders`, `/sync`, `/whoami`. Авторизация по `TELEGRAM_CHAT_ID`.
- **Main** (`src/main.py`): один asyncio-процесс, который связывает sync (APScheduler),
  watcher, telegram-бота, heartbeat и low-balance-алерт. Управляется systemd.

---

## Быстрый старт

### 1. Установить Python 3.11+

Проверь:

```powershell
python --version
```

### 2. Создать виртуальное окружение и установить зависимости

```powershell
cd D:\money
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Проверить `.env`

Файл `.env` уже создан и заполнен. Если будешь переносить проект — есть `.env.example` как шаблон.

⚠️ **`.env` содержит пароли. Никогда не коммить его в git.** Он уже в `.gitignore`.

### 4. Прогнать первую проверку доступа к ns.gifts

```powershell
python -m src.tools.check_ns
```

### 5. Проверить курс USD/RUB

```powershell
python -m src.tools.check_fx
```

### 6. Тесты (unit, без сети)

```powershell
python -m pytest tests/ -q
```

Должен показать:

```
1/4 Логин... OK
2/4 Запрос баланса... Баланс: XX.XXXX USD
3/4 Запрос каталога... N категорий, M услуг
   [17] Apple Gift Card USA
      svc_id=20    Apple Gift Card | USA | 2 USD     1.9280 USD  stock=73
   ...
4/4 Запрос курса USD->RUB... rub=XX.XX
```

#### Возможные ошибки и что делать

| Ошибка | Причина | Решение |
|---|---|---|
| `401 (auth)` | Неверный `NS_LOGIN` / `NS_PASSWORD` / `NS_API_SECRET` | Проверь `.env`, при необходимости запроси у оператора |
| `403 (forbidden)` | Твой публичный IP не в whitelist | Узнай свой IP на `2ip.ru`, напиши в саппорт ns.gifts |
| `NS_API_SECRET должен быть валидным base64` | Опечатка в секрете | Перепроверь, что вставил полную строку |

---

## Структура

```
d:\money\
├── .env                  # секреты (НЕ коммитить)
├── .env.example          # шаблон без секретов
├── .gitignore
├── README.md
├── requirements.txt
├── api-docs.md           # оригинальная док-я ns.gifts v2
├── api-playground.md     # оригинальная док-я playground
├── data/                 # БД, кэш (создаётся автоматически)
├── logs/                 # логи с ротацией
└── src/
    ├── config.py         # настройки из .env
    ├── logging_setup.py  # loguru
    ├── ns/               # клиент ns.gifts
    ├── funpay/           # клиент FunPay (F1)
    ├── db/               # SQLite модели (F2)
    ├── mapping/          # NS↔FunPay маппинги (F2)
    ├── sync/             # синхронизация каталога (F2)
    ├── orders/           # пайплайн заказов (F3)
    ├── chat/             # авто-ответы (F4)
    ├── alerts/           # Telegram (F6)
    ├── main.py                # entrypoint (запускается через systemd)
    └── tools/
        ├── check_ns.py            # проверка доступа к NS
        ├── check_funpay.py        # проверка доступа к FunPay
        ├── check_fx.py            # проверка курса USD/RUB
        ├── check_telegram.py      # тестовая отправка в Telegram
        ├── discover_chat_id.py    # авто-определение TELEGRAM_CHAT_ID
        ├── list_funpay_lots.py    # листинг лотов на FunPay
        ├── import_mappings.py     # импорт маппингов из CSV в БД
        ├── dry_run_sync.py        # прогон синхронизатора без записи на FunPay
        └── test_order.py          # ручной прогон order processor с тестовыми данными
```

---

## F2: Маппинги и синхронизация

После того как у тебя появится хотя бы один лот на FunPay:

1. Скопируй `data/mappings.example.csv` → `data/mappings.csv`, заполни своими `funpay_lot_id` ↔ `ns_service_id`.
2. Импортируй в БД:

```powershell
python -m src.tools.import_mappings data\mappings.csv
```

3. Сделай dry-run (бот покажет, что изменил бы, но **ничего не запишет** на FunPay):

```powershell
python -m src.tools.dry_run_sync
```

Когда вывод устроит — включишь `ENABLE_REAL_ACTIONS=true` в `.env`.
См. `data/README.md` для подробного описания CSV-формата.

---

## F3: Ручной тест order processor

Можно прогнать весь pipeline (без оплаты) одной командой — полезно, чтобы убедиться,
что NS принимает наш заказ для нужного `service_id`:

```powershell
python -m src.tools.test_order `
    --funpay-order-id TEST-001 `
    --funpay-lot-id 12345678 `
    --quantity 1 `
    --buyer TestBuyer
```

Без флага `--really` `pay_order` НЕ вызывается, NS отменит созданный заказ
автоматически через ~10 минут (это нормальное поведение).

---

## Telegram: настройка с нуля

1. Создай бота через **@BotFather** → получишь токен. Положи в `.env`:
   ```
   TELEGRAM_BOT_TOKEN=12345:AAA...
   ```
2. Узнай свой `chat_id` — есть две дороги:

   **A. Автоматически (рекомендуется):**
   ```powershell
   python -m src.tools.discover_chat_id
   ```
   Скрипт ждёт сообщения. Открой Telegram → найди своего бота → напиши `/start`.
   Скрипт распечатает `TELEGRAM_CHAT_ID=...` — скопируй в `.env`.

   **B. Через `@userinfobot`** — просто напиши ему любое сообщение.

3. Проверка отправки:
   ```powershell
   python -m src.tools.check_telegram
   ```

После этого `python -m src.main` поднимет всё: бот ответит на `/help`,
`/status` покажет состояние, `/sync` запустит синхронизацию вручную.

---

## Запуск 24/7

На сервере (Linux + systemd):

```bash
systemctl enable --now funpay-ns-bot
journalctl -u funpay-ns-bot -f
```

См. `deploy/README.md` — там пошагово первичная установка и обновления.

---

## Безопасность

- `ENABLE_REAL_ACTIONS=false` в `.env` — пока **выключено**, любые реальные платежи заблокированы (`pay_order` бросит исключение). Включишь, когда будешь готов к боевым покупкам.
- Если потерял `NS_API_SECRET` — оператор пересоздаст аккаунт (старый секрет не показывается повторно).
- Если протекла `FUNPAY_GOLDEN_KEY` — зайди в funpay.com, нажми "Выйти со всех устройств" и логин — это инвалидирует cookie.

---

## Что нужно от тебя дальше

Чтобы двигаться в F1 (FunPay) и F3 (реальные покупки):

1. **Внешний IP** твоего ПК/сервера → отправь в саппорт ns.gifts для whitelist.
2. **Решение по категориям**: на каких 1-2 товарах будем тестировать пайплайн? (Apple Gift Card самые простые — только `quantity`).
3. **Telegram `chat_id`**: запусти бота `@your_bot` командой `/start` после реализации F6, либо вручную напиши свой chat_id (узнать у `@userinfobot`).
