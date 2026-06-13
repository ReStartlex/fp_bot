# План улучшений NS↔FunPay Bridge

> Аудит от 2026-06-11 (commit `dd7817c`+). Каждый пункт — самостоятельное ТЗ:
> **Проблема → Где → Решение → Критерий приёмки → Сложность**.
> Порядок внутри приоритета = рекомендуемый порядок выполнения.
> После выполнения пункта — ставить `[x]` и писать commit SHA.

Контекст на момент аудита: ~110 маппингов (после миграции Steam/Apple/PS),
все активны. 1075 тестов зелёные. Продакшн: VPS 194.87.143.41, Python 3.12,
SQLite WAL, systemd (funpay-ns-bot + funpay-ns-api).

---

## P0 — масштаб и деньги (делать первыми)

### [~] P0-1. Sync не масштабируется: per-lot GET на каждый цикл — ФАЗА A DONE (`90ed865`)

**Фаза A (сделано, безопасно, оффлайн):** джиттер TTL diff-cache
(`SYNC_STOCK_DIFF_CACHE_JITTER_SECONDS`, default 60) — детерминированный
per-lot сдвиг 0..jitter в `_is_cache_hit` растягивает переоткалибровку по
окну, убирая ЗАЛП одновременных GET (корень «maximum number of running
instances»). Тесты `tests/test_sync_diff_cache_jitter.py` (7). Это снимает
burst-симптом; вместе с quick-fix .env закрывает прод-боль.

**Фаза B (в работе, поэтапно):** полноценный snapshot-sync по нодам
(1 GET `/lots/{node}/trade` на ноду вместо N offerEdit). Прод-данные
подтвердили актуальность: daily_stats 2026-06-13 r429=8, 429 идут весь
день (не только вечерний пик), источник — offerEdit, иногда offerSave и
ОПАСНО chat/?node (бьёт по delivery).

Под-этапы:
- [x] **B1 — фундамент (`3177bd7`):** колонка `Mapping.funpay_node_id`
  (nullable) + ALTER в `init_db` + параметр в `upsert_mapping`
  (None=не трогать, чтобы обычный upsert не затирал backfill).
  migrate/runner пишет node при создании. Backfill старых:
  `src/tools/backfill_node_ids.py` (GET lot_fields→node_id, throttle
  `--delay`, dry-run по умолчанию). Хелперы `set_mapping_node_id`,
  `list_mappings_missing_node_id`. Тесты `tests/test_node_id_backfill.py`
  (4). Поведение sync НЕ изменено. 1126 зелёных.
  **Деплой-чеклист B1:** после деплоя прогнать
  `python -m src.tools.backfill_node_ids --apply` на VPS (заполнит node
  для существующих ~110 лотов) — нужно ДО включения B3.
- [x] **B2 — парсер (`3f12416`):** `list_node_offers` теперь возвращает
  `price` (float, из `tc-price[data-s]`, бывает дробной 439.04) и `amount`
  (int, `tc-amount`) — этого хватает для snapshot-сравнения с target.
  Реальная вёрстка прода зафиксирована фикстурами
  `tests/fixtures/funpay/trade_node_{apple_1316,steam_1086}.html` +
  contract-тест `tests/test_list_node_offers_parse.py` (4). 1134 зелёных.
  **ВАЖНО про active:** в наблюдаемой вёрстке trade-страница перечисляет
  ТОЛЬКО активные офферы (ни одного inactive-маркера). Значит для B3:
  присутствие в snapshot = активен; отсутствие = неактивен/снят.
  Решения о ДЕАКТИВАЦИИ на snapshot НЕ опираются — B3 верифицирует
  деактивацию отдельным per-lot GET (их мало). Если у ноды появятся
  снятые лоты с иным маркером — добрать фикстуру.
- [x] **B3 — новый цикл + детектор деградации (`397662c`, за флагом):**
  В `sync_once` при diff-cache MISS вместо per-lot offerEdit GET сверяемся
  со snapshot ноды (`_snapshot_in_sync`): цена (в пределах порога) + сток
  совпали → per-lot GET ПРОПУЩЕН, last_synced переоткалиброван (snapshot —
  реальная проверка FunPay). Сток ВИДЕН в snapshot (`tc-amount`), поэтому
  отдельный «полный проход по стоку» НЕ нужен (отклонение от исходного ТЗ
  в плюс). Маппинги без node / неопределённость / деактивация → fallback
  per-lot. ДЕТЕКТОР деградации: snapshot-GET сам падает/429 на ≥
  `SYNC_SNAPSHOT_DEGRADED_NODE_THRESHOLD` (default 2) нодах → `degraded`,
  апдейты цен/стока пропускаются весь цикл (бережём rate-budget для
  chat/delivery), `_safe_sync` шлёт WARNING-алерт (анти-спам 1/час).
  За флагом `SYNC_SNAPSHOT_MODE` (default OFF). `FunPayClient.list_node_offers`
  пробрасывает исключение на 429-исчерпании (сигнал деградации, не пустой
  список). Тесты `tests/test_sync_snapshot.py` (11: юнит in-sync + 4
  интеграции). 1145 зелёных.
  **ПРИНЯТО НА ПРОДЕ (2026-06-13, SYNC_SNAPSHOT_MODE=true):**
  `checked` упал с ~64 до 0–3 за цикл, `unchanged≈113-115`,
  `snapshot_synced` стабилен, `r429=0` во всех snapshot-циклах (было
  r429=4 в старых). zombie_reaper чистый. БОЕВОЙ ТЕСТ автовыдачи —
  заказ XT8EZFFE (без lot_id, сматчен по описанию score=190;
  приветствие/PIN/благодарность через admin_http; status=delivered,
  покупатель подтвердил): без FunPayAPI-fallback / «Обновите страницу» /
  csrf-ошибок / manual_hold. Откат = `SYNC_SNAPSHOT_MODE=false` + рестарт.
  (Отдельно замечены внешние `FunPay GET 502` на `/chat/` — нестабильность
  FunPay, не snapshot-sync; детектор деградации на снапшот-нодах их не
  считает, т.к. это chat-эндпоинт.)
  Оригинальное ТЗ ниже сохранено.

#### Оригинальное ТЗ P0-1 (для фазы B)

**Проблема.** `sync_once` делает GET `offerEdit` на КАЖДЫЙ маппинг
(`src/sync/stock_sync.py`, `_decide_for_one`). Diff-cache гасит часть, но его
TTL = 120с (`sync_stock_diff_cache_ttl_seconds`), и каждые ~2 минуты у всех
лотов TTL истекает одновременно → burst из ~110 GET → шторм 429 → цикл
длится дольше `SYNC_INTERVAL_SECONDS=30` → APScheduler «maximum number of
running instances reached». Уже наблюдается в проде; с ростом лотов станет
блокером.

**Quick fix (без кода, сделать сразу).** В `.env` на VPS:
```
SYNC_INTERVAL_SECONDS=90
SYNC_STOCK_DIFF_CACHE_TTL_SECONDS=600
FUNPAY_RATE_MIN_INTERVAL_SECONDS=0.25
```
Реакция на изменение цены/стока замедлится до ~1.5 мин — приемлемо.

**Правильное решение: snapshot-sync по нодам.**
1. Добавить `Mapping.funpay_node_id` (nullable int) в `src/db/models.py`
   + ALTER TABLE в `init_db` (миграций нет — добавлять колонку через
   `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`, по образцу как
   добавлялись `last_synced_*`, если такого механизма нет — написать).
   Заполнение: `migrate/runner.py` знает node при создании; для старых —
   разовый скрипт через `KnownLot`/`get_lot_summary` (subcategory_id).
2. В `sync_once`: сгруппировать маппинги по node. Для каждой ноды —
   ОДИН GET `/lots/{node}/trade` (расширить
   `admin_http.list_node_offers`, чтобы парсить ещё и цену лота из строки —
   она есть в разметке `tc-price`). Получаем (active, price) всех лотов ноды.
3. Сравнить snapshot с target. GET `offerEdit` + `save_lot` — ТОЛЬКО для
   лотов, требующих изменения. Сток в snapshot не виден — но сток меняется
   только после продажи (есть invalidate-hook) и при изменении NS-остатка
   ниже cap; для перепроверки стока оставить редкий полный проход
   (раз в N циклов) или GET только лотов с продажами.
4. Маппинги без `funpay_node_id` — fallback на старый per-lot путь.

**Приёмка.** Стабильный цикл при 110 лотах: ≤ (число нод + число изменений)
FunPay-запросов; в логах `Sync done ... http=[... r429=0 ...]` в норме;
предупреждение «maximum number of running instances» исчезло из journalctl.

**Сложность:** средняя-высокая (2-4 часа модели + прод-проверка).
Файлы: `src/sync/stock_sync.py`, `src/funpay/admin_http.py`,
`src/db/models.py`, `src/db/repo.py`, `src/migrate/runner.py`, тесты.

---

### [x] P0-2. Контракт ответов FunPay не зафиксирован фикстурами — DONE (`abf0838`)

Сделано: `classify_offersave_response` вынесена чистой функцией; не-JSON
ответ = успех ТОЛЬКО на чистом 3xx-редиректе (200-HTML/мусор → ok=False);
логин-форма (body или Location) → ok=False с маркером протухшей сессии.
Фикстуры `tests/fixtures/funpay/offersave_*.json|login_redirect.html` +
`tests/test_funpay_contract.py` (11 тестов). Полный прогон 1086 зелёных.

**Проблема.** Парсинг ответов `offerSave` — эвристики
(`admin_http.save_lot`: «JSON без msg/error = ок», «HTML без слова "ошибк" =
ок»). Уже дважды стреляло (инцидент amount=0; price=999999). JSON-ветка
ужесточена (error/errors), но HTML-ветка осталась хрупкой, и при смене
вёрстки FunPay узнаем по тихим сбоям.

**Решение.**
1. Собрать реальные ответы FunPay в `tests/fixtures/funpay/`:
   `offersave_ok.json`, `offersave_validation_error.json` (есть в логах:
   `{"done":false,"error":"Заполните все поля.","errors":[["price","Неверная цена."]]}`),
   `offersave_429.txt`, `offeredit_form.html` (урезанный),
   `login_redirect.html`. Дополнять при каждом новом инциденте.
2. Contract-тесты: `save_lot`/`get_lot_fields`/`list_node_offers` парсят
   фикстуры и дают ожидаемый `ok`/поля.
3. HTML-ветку `save_lot` сделать строже: HTML-ответ на offerSave считать
   успехом ТОЛЬКО если это redirect на страницу лота (проверять
   `Location`/маркер), иначе `ok=False` + body_preview в лог.

**Приёмка.** Тесты падают, если эвристика парсера меняет поведение на
зафиксированных реальных ответах. HTML-ответ неизвестного вида → ok=False.

**Сложность:** низкая-средняя. Файлы: `src/funpay/admin_http.py`,
`tests/fixtures/funpay/*`, новый `tests/test_funpay_contract.py`.

---

### [x] P0-3. Нет проактивного watchdog'а протухания golden_key — DONE (`8df7aad`)

**Follow-up (инцидент 2026-06-13, `95e8f63`): устранён false positive.**
После деплоя watchdog прислал «golden_key протух» по ОДНОМУ whoami-fail,
хотя рядом save_lot/sync работали авторизованно. Причины две: (1) `whoami`
не отличал «точно разлогинены» (форма/редирект `/account/login`) от «не
смогли распарсить user_id» (транзиент/смена вёрстки) — теперь отдаёт
явный `login_marker`; (2) алерт летел с первого же сбоя. Исправлено:
`check_auth()` → `(status, reason)` где status ∈ `authed`/`logged_out`/
`unknown` (unknown НЕ трактуется как разлогин); streak-механизм
`_register_funpay_auth_failure/_ok` — алерт только при
`FUNPAY_AUTH_WATCHDOG_CONFIRM_FAILURES` (default 2) подтверждениях из
разных источников/циклов, любой удачный авторизованный sync (http.ok>0,
auth_errors=0) сбрасывает streak. Тесты: +5 в test_funpay_auth_watchdog.py
(трёхзначный check_auth + streak-порог + recovery). 1110 зелёных.

Сделано: job `_funpay_auth_watchdog` (каждые 600с, `FUNPAY_AUTH_WATCHDOG_*`)
дёргает `FunPayClient.check_auth()` (whoami) → при потере авторизации алерт
с инструкцией обновить golden_key (анти-спам по кулдауну, по умолчанию 1ч),
на восстановлении — «✅». Плюс немедленный сигнал: `sync_once` помечает
лоты `auth_error` (FunPayAuthError) и возвращает `result["auth_errors"]`,
`_safe_sync` алертит сразу тем же `_alert_golden_key_expired`. Тесты:
`tests/test_funpay_auth_watchdog.py` (4). 1090 зелёных.

**Проблема.** При инвалидации `golden_key` FunPay-клиент существует
(`fp is not None`), поэтому `_funpay_reconnect_if_needed` (`src/main.py:470`)
ничего не делает — он чинит только «клиент не создан». Все операции начинают
падать auth-ошибками; оператор узнаёт по косвенным алертам.
`zombie_reaper`/`sync` алертят на 3+ подряд падений, но текст не говорит
«обнови golden_key».

**Решение.** Периодический job (раз в 5-10 мин): `admin.whoami()` →
если `authenticated=False` / редирект на логин — однозначный алерт
«🔑 golden_key протух, обнови в .env и перезапусти» (анти-спам: раз в час).
Дополнительно: ловить `FunPayAuthError` в `_safe_sync` и алертить с тем же
текстом сразу.

**Приёмка.** При подмене golden_key на мусор алерт с инструкцией приходит
в течение ≤10 минут, ровно один (не каждые 30с).

**Сложность:** низкая. Файлы: `src/main.py`, `src/alerts/telegram.py` (текст).

---

### [x] P0-4. Цены/сток: верификация записей не покрывает price/stock — DONE (`591d963`)

Сделано (дёшево, без extra GET): `_decide_for_one` сравнивает текущую цену
FunPay с `last_synced_price` (что бот записал в прошлый раз); расхождение
>1% → `decision.price_mismatch` + WARNING-строка в лог. `sync_once` агрегирует
`result["price_mismatches"]`, `_safe_sync` шлёт WARNING-алерт (анти-спам 1/час).
Тесты `tests/test_sync_price_mismatch.py` (3). 1093 зелёных.

**Проблема.** Verify-after-save сделан только для деактивации
(`stock_sync._apply_decision`). Запись цены/стока верит ответу offerSave.
После P0-2 (строгий парсер) риск мал, но «FunPay ответил ок и не применил»
уже наблюдался — для цены это прямой убыток при падении курса.

**Решение (дёшево).** Diff-cache уже честно ре-калибруется по TTL — добавить
проверку расхождения: если на re-GET текущая цена ≠ last_synced_price
существенно (> порога) И мы её недавно «успешно» писали — WARNING-алерт
«FunPay не применил цену лота N». Не чинить молча, а сигналить (1 шт/час).

**Приёмка.** Тест: save_lot «успешен», но фейк-FunPay цену не применил →
на следующем цикле в логе WARNING с lot_id.

**Сложность:** низкая. Файлы: `src/sync/stock_sync.py`, тест.

---

## P1 — корректность и сопровождаемость

### [~] P1-1. Разбить `orders/processor.py` (1551→764) — ИНКР. 1-3 DONE (`6bd903e`/`cca52fa`/`14a9857`)

Монолит со стадиями create/pay/wait/deliver/holds/refund в одной функции
`_process_locked` + вложенные хелперы. Любая правка рискует задеть соседний
сценарий (см. историю аудитов #1-#8 в комментариях).

**Решение.** Выделить модули `src/orders/stages/`:
`resolve.py` (маппинг, AmbiguousMatch), `purchase.py` (NS create/pay/wait),
`delivery.py` (двухфазная доставка), `holds.py` (manual_hold/_mark_failed/
_emergency_disable_lot). `processor.py` остаётся оркестратором.
Поведение НЕ менять — только перенос; тесты должны пройти.

**Прогресс (поэтапно, каждый инкремент = коммит + полный прогон):**
- [x] Инкр. 1 (`6bd903e`): `events.py` (FunPayOrderEvent, ре-экспорт из
  processor) + `stages/resolve.py` (матчинг + `_resolve_mapping`/
  `_resolve_chat_id`). 1551→1260. Парный патч `resolve.session_factory`.
- [x] Инкр. 2 (`cca52fa`): `stages/common.py` (`_pins_from_order`/
  `_order_age_seconds`) + `stages/holds.py` (`_trigger_manual_hold`/
  `_mark_failed`/`_emergency_disable_lot`). 1260→1029. Ре-экспорт +
  патч `holds.session_factory`. `_is_hard_timeout` оставлен в processor
  (патчится тестами).
- [x] Инкр. 3 (`14a9857`): `stages/delivery.py` (`_should_hold_delivery`/
  `_deliver_pins`). 1029→764. get_usd_rub_rate переехал в delivery
  (патч-таргет в 12 тест-местах обновлён) + патч `delivery.session_factory`.
- [~] Инкр. 4 (purchase) — **ОТЛОЖЕНО (осознанно).** Извлечь NS
  create/pay/wait из `_process_locked` — это НЕ чистый перенос, а
  экстракция из единого try-блока с ~10 разделяемыми локалами,
  переплетёнными с hard-timeout/manual_hold/сессиями. Риск регрессии на
  денежном пути выше выигрыша; то, что осталось в processor (764 стр.) —
  это и есть «оркестратор», который план просил оставить. Возвращаться
  только при явной необходимости и с отдельным дизайн-ревью шва.

**Приёмка.** Достигнуто по духу: монолит 1551→764 (−51%), все
обособляемые стадии (resolve/holds/delivery) вынесены в `stages/`,
поведение = 0 (1145 тестов зелёные на каждом инкременте). Формальный
порог «<400 строк» НЕ достигнут — требует рискованной экстракции
purchase (инкр. 4, отложен).

**Сложность:** средняя (инкр. 1–3 — механические; инкр. 4 — рискованный).

---

### [~] P1-2. Выпилить библиотеку FunPayAPI — ЭТАП 2 DONE (`a4756f5`)

**Проблема.** От FunPayAPI осталось: `get_sells` (snapshot/recent sales),
`send_message` (с fallback на admin_http), `get_user/get_lots`, и ради неё —
глобальный monkeypatch `requests.Session.request`
(`src/funpay/client.py:107-217`) + пин `requests==2.28.1`/`urllib3 1.26`
(старые, с известными CVE). Библиотека неофициальная, ломается при смене
вёрстки.

**Решение.** Поэтапно: (1) `get_sells` → собственный парсер
`/orders/trade` в admin_http (по образцу `list_node_offers`); (2)
`send_message` → admin_http уже умеет (`send_chat_message`), сделать его
основным путём; (3) `get_my_lots` → `list_node_offers` по нодам; (4) удалить
monkeypatch и зависимость, поднять requests/urllib3.
Каждый этап — отдельный коммит с фикстурными тестами (см. P0-2).

**Прогресс:**
- [x] Этап 2 (`a4756f5` + csrf-фикс `6910849`): `send_message` развёрнут —
  ОСНОВНОЙ путь теперь `admin_http.send_chat_message` (прямой POST, без
  хрупкого HTML-парсинга ответа; csrf из `body[data-app-data]`). FunPayAPI
  оставлен РЕЗЕРВОМ на пару деплой-циклов (пропуск доставки страшнее дубля);
  снимется под-этапом 2b после ≥3 дней без регрессий. Тесты
  `test_send_message_fallback.py` переписаны под admin_http-primary (8).
  **ПОДТВЕРЖДЕНО НА ПРОДЕ:** заказ W9MJC5MA (после деплоя `96bcbfa`) — все
  3 сообщения (приветствие/PIN/после подтверждения) ушли
  `[via admin_http]`, без «Обновите страницу»/fallback, статус delivered.
- [ ] Под-этап 2b: убрать FunPayAPI-резерв из `send_message` совсем.
- [ ] Этап 1: `get_sells` → парсер `/orders/trade` в admin_http.
- [ ] Этап 3: `get_my_lots` → `list_node_offers` по нодам.
- [ ] Этап 4: удалить monkeypatch + зависимость, поднять requests/urllib3.

**Приёмка.** `pip show FunPayAPI` отсутствует; monkeypatch удалён;
заказы/чат/санк работают на проде ≥ 3 дней без регрессий.

**Сложность:** высокая (делать поэтапно, не одним PR).

---

### [x] P1-3. `datetime.utcnow()` → `datetime.now(UTC)` (36 вхождений) — DONE (`e1c01b6`)

Сделано: `src/timeutil.utcnow()` возвращает naive-UTC
(`datetime.now(timezone.utc).replace(tzinfo=None)`) — точная семантика
старого вызова, без deprecation и без смешивания aware/naive (SQLite
хранит naive). Заменены все 36 вызовов в 11 файлах. 0 вхождений
`datetime.utcnow()` (кроме docstring helper'а). 1099 зелёных, все
модули импортируются.

Deprecated в Python 3.12 (прод на 3.12). Naive-datetime уже почти укусил
(сравнения в diff-cache). Механическая замена + проверить места сравнения
с БД-значениями (SQLite хранит naive — при замене использовать
`datetime.now(UTC).replace(tzinfo=None)` или конвертировать обе стороны;
НЕ смешивать aware/naive).

**Приёмка.** 0 вхождений `utcnow`; тесты зелёные; нет
`TypeError: can't compare offset-naive and offset-aware`.

**Сложность:** низкая, но внимательная.

---

### [x] P1-4. KnownLot для мигрированных лотов — DONE (`aeb4542`)

Сделано: `upsert_known_lot` в repo; `migrate/runner` пишет KnownLot с
title = подставленный summary_ru сразу при создании лота (mark_notified,
чтобы discovery не шумел). Backfill для старых: `src/tools/backfill_known_lots.py`
(title из mapping.label, dry-run по умолчанию, `--apply`). Тест в
test_migrate_runner.py. 1099 зелёных.

**Проблема.** Матчинг заказов без lot_id использует `KnownLot.title`
(бонус 120 в `_mapping_match_score`). Лоты, созданные миграцией, получают
KnownLot только когда их заметит `new_lots` discovery — а до того матчинг
опирается лишь на label. При ~110 похожих лотах каждый источник сигнала важен.

**Решение.** В `migrate/runner.py` после создания лота сразу писать
`KnownLot(funpay_lot_id, title=<summary_ru с подставленными тегами>)`.
Разово добежать по существующим: скрипт, который для маппингов без KnownLot
берёт title из `list_node_offers`.

**Приёмка.** Каждый маппинг имеет KnownLot с непустым title (проверочный
запрос в тесте/скрипте).

**Сложность:** низкая.

---

### [x] P1-5. Аудит «глотающих» except (205 вхождений) — DONE (`d85dbfa`)

**Аудит денежных путей (просмотрено):**
- `orders/processor.py` — БЕЗОПАСНО. `delivered` ставится только ПОСЛЕ
  успешного send_message (двухфазно delivering→delivered). Сбои → явные
  статусы manual_hold/pins_ready/failed + emergency_disable + алерт. Все
  generic except на некритичных шагах (приветствие, расчёт прибыли,
  инвалидация кэша, emergency_disable) не влияют на статус заказа.
- `orders/processor.py::_emergency_disable_lot` — БЕЗОПАСНО (verify-after-
  save + mapping disable в БД даже при сбое save_lot, аудит #8).
- `shop/delivery.py::_finalize_failure` — НАЙДЕНА И ИСПРАВЛЕНА дыра:
  при провале `refund_failed_order` ошибка только логировалась, а
  покупателю всё равно слался «средства возвращены» (ложь, тихая потеря
  денег). Теперь: при провале возврата — 🚨-алерт владельцу «верни
  вручную», покупателю — нейтральный текст «возврат обрабатывается».
  refund идемпотентен → ручной/повторный возврат безопасны.
- `shop/payments/*` — CryptoBot webhook подписан (hmac), poller
  идемпотентен по invoice_id; начисление в транзакции. Безопасно.

**Сложность:** средняя (вдумчивое чтение). Сделана 1 точечная правка.

Не менять массово. Пройти точечно денежные пути: `orders/processor.py`,
`shop/delivery.py`, `shop/payments/*` — убедиться, что ни один `except
Exception` не превращает неуспешную выдачу/оплату в «тихий успех».
Для каждого подозрительного: либо алерт, либо статус-машина (manual_hold),
либо комментарий-обоснование.

**Приёмка.** Документированный список просмотренных мест (комментарий
`# audited: ...` или запись в этом файле).

**Сложность:** средняя (вдумчивое чтение).

---

## P2 — платформа и гигиена

### [ ] P2-1. Разбить `alerts/bot.py` (4969 строк, 195 def)

Те же мотивы, что P1-1. Разнести на `src/alerts/handlers/` по разделам меню
(status, lots, mappings, orders, groups, settings, shop_admin). Сессии
пагинации и `_guard` — в общий модуль.

**Приёмка.** Ни один файл > 1000 строк; тесты зелёные.

### [x] P2-2. CI (GitHub Actions) — DONE (`<pending>`)

Сделано: `.github/workflows/ci.yml` — matrix Python 3.11+3.12, install
requirements, `pytest -q`. Обязательные Settings-поля заданы dummy-env
(в CI нет .env; ns_api_secret=base64 QQ==). Локально проверено: с этими
env и без .env тесты собираются и проходят. Финальный зелёный прогон
Actions подтвердится после первого push в GitHub.

Сейчас тесты гоняются только вручную. Добавить `.github/workflows/ci.yml`:
python 3.11 + 3.12, `pip install -r requirements.txt`, `pytest -q`.
Секреты не нужны (тесты оффлайн).

**Приёмка.** PR/push в ветку — автоматический зелёный прогон.

### [x] P2-3. BUILD_INFO пишет неверную ветку — DONE (`c7327c8`, вместе с P2-4)

В проде `BUILD_INFO: branch=main`, хотя деплой из
`vps-stable-2026-06-02-151807`. Вводит в заблуждение при диагностике.
Поправить генерацию BUILD_INFO в `deploy/fetch_code.sh` (брать фактический
ref/branch, а не дефолт).

**Приёмка.** После деплоя `cat BUILD_INFO` показывает реальную ветку.

### [x] P2-4. Зафиксировать ветку деплоя — DONE (`c7327c8`)

P2-3 и P2-4 имели общий корень: `BRANCH` дефолтил в `main`. Теперь
`fetch_code.sh` резолвит BRANCH: env > `.deploy_branch` > main, и при
явном `BRANCH=` персистит её в `${PROD_APP_DIR}/.deploy_branch` (исключён
из git clean, как .deploy_pin). Это чинит и откат-на-main, и
`BUILD_INFO branch=`. Один раз задеплоить с `BRANCH=vps-stable-...`, далее
`bash deploy/update.sh` без env берёт закреплённую ветку. 4 статических
теста в test_deploy_scripts_safety.py. 1102 зелёных.

Каждый деплой требует `BRANCH=vps-stable-... PIN_SHA=...` вручную — забыть
`BRANCH` = откат на main. Решение: писать ветку в `.deploy_branch` рядом с
`.deploy_pin`, `fetch_code.sh` читает её как дефолт.

**Приёмка.** `bash deploy/update.sh` без env-переменных обновляет на
последний коммит закреплённой ветки.

### [x] P2-5. Наблюдаемость: счётчики в БД вместо grep по журналу — DONE (`823d4c7`)

Сделано: таблица `daily_stats(day, r429, exhausted, deactivations)` для
рантайм-сигналов, которых нет в других таблицах. Исходы заказов
(ok/failed/manual_hold/pins_ready) НЕ дублируются — выводятся из `orders`
по `func.date(created_at)` (идемпотентно, без двойного учёта при
ре-обработке, без правок денежного пути). `repo.bump_daily_stats` —
SQLite-upsert (аддитивный, нулевой вызов = no-op); `repo.get_daily_summary`
объединяет вывод заказов + счётчики. Инкремент best-effort из `_safe_sync`
(r429/exhausted/деактивации) и zombie-reaper-job (деактивации) — сбой
учёта НЕ влияет на sync. Показ: Telegram `/status` (блок «📊 Статистика
сегодня/вчера») + `/api/dashboard` (ключ `daily.today/yesterday`). Тесты
`tests/test_daily_stats.py` (5: аддитивность, no-op, вывод исходов,
разделение суток, пустой день). 1122 зелёных.

Сводки уже пишутся в лог (sync done, http metrics, reaper). Добавить
лёгкую таблицу `daily_stats` (дата, orders_ok, orders_failed, manual_holds,
ambiguous_matches, r429, exhausted, deactivations) и показывать в
`/api/dashboard` + Telegram `/status`. Это превратит «grep journalctl» в
«один взгляд».

**Приёмка.** `/status` показывает счётчики за сегодня/вчера.

### [x] P2-6. Rate-limit на вход site-API — DONE (`c8836b3`)

Сделано: `src/api/ratelimit.py` — InMemoryRateLimiter (скользящее окно
per-key `path:ip`, IP из X-Forwarded-For), dependency `auth_rate_limit`
на `/auth/telegram|webapp|oauth`. Лимитер на `app.state` (тесты не
пересекаются). Настройки `SITE_AUTH_RATE_LIMIT` (10), `SITE_AUTH_RATE_
WINDOW_SECONDS` (60), 0=выкл. > N/мин с IP → 429. Тесты
`tests/test_api_ratelimit.py` (3). 1105 зелёных.

`src/api/site_router.py` — публичные эндпоинты логина (Google/Yandex/
Telegram initData). Проверить и добавить простейший rate-limit
(IP+endpoint, в памяти) на auth-ручки, чтобы не дать брутфорсить.
Webhook CryptoBot подписан (ок), Telegram login — HMAC (ок).

**Приёмка.** > N запросов/мин с одного IP на /auth → 429.

### [ ] P2-7. Кастомные номиналы FunPay («Другое количество»)

Отложенные товары: Apple 6/7/8/9/25 USD; PS 120/150/200 GBP. Механизм:
селект = «Другое количество» + парное текстовое поле (`fields[usd2]`,
`fields[gbquantity]`+`fields[gb...2]`? — уточнить по схеме). Добавить в
`migrate/config.py` опцию `custom_amount_field` на валюту: если номинала
нет в селекте — класть «Другое количество» в селект и число в кастомное
поле. Проверять на 1 лоте перед волной.

**Приёмка.** Apple 25 USD создан через кастомное поле, отображается
корректно в UI, продаётся.

---

## P3 — будущая фича: прямые пополнения (top-up по UID)

Цель: продавать NS-категории с `order-fields: account_number*` (Free Fire,
PUBG Mobile, MLBB и т.д.) — после оплаты покупатель присылает UID/логин,
бот делает NS-заказ с этими полями.

**Что уже есть (фундамент):**
- NS `FieldType` отдаёт схему полей категории: key, required, regex, enum
  (`src/ns/models.py:11`) — валидация входа покупателя готова из коробки.
- `Mapping.ns_fields_template` поддерживает подстановки (`@QUANTITY`) —
  расширяется до `@BUYER[account_number]`.
- Shop-бот уже собирает поля покупателя при checkout
  (`f93e0ac feat(checkout)`) — переиспользовать UX-логику и валидацию.
- `manual_hold` — готовый механизм для «UID невалиден / не прислал».

**Дизайн (утвердить до реализации):**
1. Новый статус заказа `awaiting_buyer_data` (после оплаты, до NS create).
2. ChatHandler: state-machine на чат заказа — приветствие с просьбой
   прислать UID (шаблон из `chat/templates.py`), парсинг ответа,
   валидация regex/enum из NS FieldType, подтверждение покупателю.
3. Таймаут ожидания (например 24ч) → напоминание → manual_hold.
4. Невалидный ввод ×3 → manual_hold с алертом.
5. `migrate`: снять ограничение `ELIGIBLE_ORDER_FIELDS` для этих категорий
   (новый класс пригодности `buyer-data`), шаблоны описаний с инструкцией
   «после оплаты отправьте UID в чат».
6. КРИТИЧНО: NS-заказ на пополнение невозвратен при неверном UID —
   перед `pay_order` отправлять покупателю «Пополняю аккаунт <UID>,
   подтвердите» (кнопка/слово «да»), и только потом платить.

**Сложность:** высокая, отдельный проект. Не начинать без отдельного
дизайн-ревью этого раздела.

---

## Быстрые правки конфига (сделать сегодня, без кода)

```bash
# /opt/funpay-ns-bot/.env
SYNC_INTERVAL_SECONDS=90              # было 30: 110 лотов не успевают
SYNC_STOCK_DIFF_CACHE_TTL_SECONDS=600 # было 120: реже полные перечитки
FUNPAY_RATE_MIN_INTERVAL_SECONDS=0.25 # было 0.1: меньше 429
systemctl restart funpay-ns-bot
```
Проверка через час: `journalctl -u funpay-ns-bot --since '1 hour ago' | grep -cE "429|maximum number"` — должно стремиться к нулю.

---

## Инциденты и точечные фиксы (вне нумерованного плана)

### Заказ JK6JW57J (2026-06-13, `3a27f5f`): send_message залипал на протухшем CSRF

При доставке (20 pins) `send_message` исчерпал ретраи на ответе FunPay
`{"msg":"Обновите страницу и повторите попытку.","error":1}` (HTTP 400) —
заказ пришлось выдать вручную. Корень: `_ensure_csrf` кэшировал
csrf-токен на весь процесс и НЕ перевыпускал его; FunPay ротирует токен в
течение жизни процесса, и ретраи слали тот же мёртвый токен → вечное
«Обновите страницу» до перезапуска. Фикс: `_invalidate_csrf()` +
детектор `_looks_like_stale_csrf` — при таком ответе сбрасываем кэш csrf,
следующая попытка перевыпускает токен через whoami. Тесты:
`tests/test_send_message_csrf_refresh.py`. save_lot НЕ затронут — он
берёт csrf из свежесчитанной формы лота, и verify-after-save штатно
поймал неприменённую аварийную деактивацию (→ pins_ready + алерт).

### Заказ NTZ3MLCY (2026-06-13, `6910849`): csrf брался из НЕ ТОГО источника

Доделка к JK6JW57J. После деплоя `eab990e` доставка снова ушла через
FunPayAPI-резерв: admin_http исчерпал 3 попытки с «Обновите страницу»
ДАЖЕ после перевыпуска токена. Корень: `whoami` тянул csrf из
`meta[name=csrf-token]`, а `/runner/` (chat_message) валидирует ДРУГОЙ
токен — из `body[data-app-data]` JSON (поле `csrf-token`). Перевыпуск
давал тот же неподходящий meta-токен → вечная «Обновите страницу».
Фикс: `whoami` и fallback `_ensure_csrf` (/chat/) теперь берут csrf из
`body[data-app-data]` (тот же источник, что FunPayAPI
`Account.app_data["csrf-token"]`), meta/input — только деградация.
Заодно userId парсится из app-data. Тесты: +2 whoami-теста. 1117 зелёных.
Резерв FunPayAPI в send_message трогать НЕ будем, пока admin_http не
подтвердит primary-доставку на проде (acceptance этапа 2 P1-2).

### zombie_reaper долбил удалённый лот (id=49 / 69932320, `253bbad`)

Всплыло при backfill node_id (B1): удалённый вручную FunPay-лот
69932320 (disabled mapping id=49, last_synced_active=0) бесконечно
брался zombie_reaper'ом → GET падал → копились errors. Попытка sentinel
funpay_lot_id=0 дала «GET lot 0: обязателен node_id».

Фикс фильтра кандидатов reaper'а (НЕ `enabled=1` — это убило бы саму
ловлю зомби!): `enabled=False AND funpay_lot_id > 0 AND
last_synced_active IS NOT 0`. last_synced_active=0 = мы уже знаем лот
неактивным/удалённым → зомби нет. Настоящий зомби (проваленный
save(active=False)) имеет last_synced_active 1/NULL → берётся как раньше.
Аналогично `backfill_node_ids` теперь пропускает disabled и
funpay_lot_id<=0 (`list_mappings_missing_node_id`). Тесты: +4. 1130
зелёных. ОСТАЁТСЯ (B2/B3): надёжное детектирование «лот удалён» по
ответу FunPay (404/parse) для лотов с last_synced_active≠0.

### P&L по заказу: сохранённая прибыль + breakdown (2026-06-13, `<pending>`)

Запрошенное улучшение (холодный расчёт, без внешних запросов в момент
выдачи). Прибыль считается из УЖЕ известных данных:
`profit = sold_rub - sold_rub*fee_rate - ns_price_usd*fx_at_sale`.
- `mapping/rules.compute_profit_breakdown` — Decimal на всех денежных
  шагах, quantize до копеек; None при нехватке данных (выдача не ломается).
- `Order` + колонки `cost_rub`, `funpay_fee_rub` (+ ALTER); `sold_rub` =
  `funpay_price_rub`, `usd_rub_rate_at_sale` = `fx_rate_at_sale`
  (переиспользованы, без дублей). Ставятся при delivered.
- Комиссия — существующая `FUNPAY_WITHDRAWAL_FEE_PERCENT=3.0` (3% ≡ 0.03;
  НЕ добавлял parallel FEE_RATE — один источник истины для денег).
- `order_success`: строка «Прибыль: X₽» / «n/a».
- Сводки (`/stats`, `/api/dashboard`) через `order_financials`:
  СУММИРУЮТ сохранённый `profit_rub`, не пересчитывают по текущему курсу;
  для старых заказов без сохранённого — fallback по сохранённому fx.
- Тесты `tests/test_order_profit.py` (8). 1153 зелёных.

## Что НЕ трогать (работает, проверено в этой итерации)

- Деактивация лотов (active-only + verify-after-save) — починено `2d90d01`.
- Матчинг заказов (вето валюта/номинал, word-boundary, manual_hold) — `18c16ea`.
- Telegram через SOCKS5 (оба aiogram-бота) — `dc3f369`.
- Конвейер миграции `src/migrate/*` — рабочий, идемпотентный.
- deploy/update.sh (staging+verify+backup+rollback hint) — образцовый.
- Двухфазная доставка, manual_hold, guardrails маржи — не упрощать.
