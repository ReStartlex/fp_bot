# NS.gifts ⇄ FunPay Bridge

Бот для автоматизации перепродажи цифровых товаров с **ns.gifts** (опт) на **FunPay** (розница).

## Что делает

- Синхронизирует каталог ns.gifts → FunPay (цены, остатки, активация лотов) с твоей наценкой.
- При покупке на FunPay автоматически покупает товар на ns.gifts и отправляет код покупателю в чат.
- Отвечает покупателям красивыми шаблонами, просит отзыв после продажи.
- Шлёт алерты в Telegram (через прокси для РФ).

---

## Текущая стадия

**F0–F2 готовы.** Реализовано:

- F0: Конфиг (`src/config.py`), NS-клиент с HMAC + auto-refresh + ретраями (`src/ns/`), `check_ns` CLI.
- F1: FunPay-клиент-обёртка (`src/funpay/`), CLI: `check_funpay`, `list_funpay_lots`.
- F2: SQLite (SQLAlchemy + aiosqlite) с моделями `Mapping`, `Order`, `FxRate`, `SyncRun`.
  CSV-импорт маппингов. Получение курса USD/RUB с ЦБ РФ (cbr-xml-daily) с кэшем.
  Расчёт целевой цены/стока с наценкой и порогом обновления.
  Sync engine (dry-run и реальный).

Дальше: F3 (обработка заказов FunPay → NS → доставка) и F4–F6 (чат-автоответчик,
планировщик, Telegram-алерты).

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
    └── tools/
        ├── check_ns.py            # проверка доступа к NS
        ├── check_funpay.py        # проверка доступа к FunPay
        ├── check_fx.py            # проверка курса USD/RUB
        ├── list_funpay_lots.py    # листинг лотов на FunPay
        ├── import_mappings.py     # импорт маппингов из CSV в БД
        └── dry_run_sync.py        # прогон синхронизатора без записи на FunPay
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
