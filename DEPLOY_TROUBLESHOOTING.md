# DEPLOY_TROUBLESHOOTING.md

> Дерево решений для обновления VPS, когда «git не качает».
> Журнал «что пробовал» — внизу. **Дополняй после каждой попытки**, чтобы
> следующий чат не наступал на те же грабли.

---

## ⚡ TL;DR — стандартный релиз (90% случаев)

> **Открыл новый чат, надо задеплоить свежий коммит?** Сначала прочитай
> этот блок, §0.1 «Грабли» и §0.2 «Зеркала GitHub».
> Команды ниже — проверены на Timeweb VPS, обновляются при каждом инциденте.
>
> **Phase 1 (TG-shop)?** Перед первым деплоем shop-фазы — см. §0.4 «Включение Shop».

```bash
APP=/opt/funpay-ns-bot
SHA=<полный_40-символьный_SHA_нового_коммита>

# 1) Pin на нужный коммит (полный SHA, не короткий — надёжнее).
#    Если деплоишь свежий main без pin'а — пропусти шаг 1 и
#    обязательно ВЫПОЛНИ `sudo rm -f "$APP/.deploy_pin"` (см. §0.1).
echo "$SHA" | sudo tee "$APP/.deploy_pin"
sudo chown bot:bot "$APP/.deploy_pin"

# 2) Чистим прошлый staging (если предыдущий деплой упал на verify).
sudo rm -rf /opt/funpay-ns-bot.staging

# 3) Обновление через рабочее GitHub-зеркало. ОБЯЗАТЕЛЬНО `bash` префиксом.
#    ⚠ ВСЕГДА проверяй §0.2 — gh-proxy.com периодически уходит в техработы.
#    На дату последнего инцидента (2026-05-25) живо: gh.idayer.com.
sudo GH_PROXY=https://gh.idayer.com bash "$APP/deploy/update.sh"

# 4) Проверка
cat "$APP/BUILD_INFO"
sudo systemctl status funpay-ns-bot --no-pager | head -20
```

`update.sh` сделает: бэкап `.env` + БД → fetch в staging → verify
(compileall, наличие `src/main.py`) → stop сервиса → rsync staging →
pip install → start. **Если fetch упал — бот продолжит работать
на старой версии** (staging-pattern).

---

## 0.1 Грабли, которые я (ассистент) повторяю чаще всего

Если ты читаешь это в новом чате — **не повторяй эти ошибки**:

| Грабля | Почему ломается | Правильно |
|---|---|---|
| `git -c http.proxy=https://gh-proxy.com fetch ...` | gh-proxy.com — **НЕ HTTP-прокси**, а URL-префикс зеркалирования GitHub. Git пытается сделать `CONNECT gh-proxy.com:443` через HTTP-прокси-протокол → виснет на 120-180с до timeout. | НЕ задавать прокси совсем. `fetch_code.sh` сам подставит `https://gh-proxy.com/https://github.com/...` как URL-prefix. |
| `GIT_HTTP_PROXY='socks5h://...@166.88.218.111:62947' bash update.sh` | Прокси с Timeweb VPS недоступен на TCP-уровне (egress-блок к этому IP). Висит → timeout. | Запускать `update.sh` **без env-переменных**. Default path через gh-proxy работает. |
| `git fetch --prune` руками | На VPS лишний шаг, ничего не даёт — `update.sh` всё делает сам в staging. | Только `sudo bash $APP/deploy/update.sh`. |
| Pin коротким SHA `3c3be85` | Иногда не резолвится в `git fetch <sha>` если объект ещё не подкачан. | Всегда полный 40-символьный SHA. `git rev-parse HEAD` локально. |
| Запускать команды на VPS из `sudo -u bot` для shell-операций с `>` | Перенаправление `>` выполняется shell'ом до `sudo`, файл создаётся от root. | `echo SHA \| sudo tee $APP/.deploy_pin` + `sudo chown bot:bot`. |
| `sudo $APP/deploy/update.sh` (без `bash`) | Скрипт **не имеет +x в репо** (rsync через `update.sh` сохраняет нынешние permissions). VPS-консоль отвечает: `sudo: /opt/funpay-ns-bot/deploy/update.sh: command not found`. | **Всегда** `sudo bash "$APP/deploy/update.sh"` (или `sudo GH_PROXY=... bash "$APP/deploy/update.sh"`). |
| Забыть про `.deploy_pin` от прошлой сессии | Если pin указывает на старый SHA, любой будущий `update.sh` будет тянуть этот старый SHA, а не свежий main. Будет деплоиться «то же, что уже стоит». | Перед деплоем: `sudo rm -f "$APP/.deploy_pin"` (или прописать туда **новый** полный SHA). |
| Забыть удалить `/opt/funpay-ns-bot.staging` | Остатки прошлого staging'а (особенно если он упал на verify) могут конфликтовать с rsync. | `sudo rm -rf /opt/funpay-ns-bot.staging` перед каждым `update.sh`. |
| Передавать `PROD_APP_DIR=/opt/funpay-ns-bot` env | Не нужно: `update.sh` сам определяет prod-каталог как `$APP_DIR` исходя из своего пути. Лишняя переменная путает чтение. | Опускать. |

**Если повторил какую-то из этих ошибок — это сигнал перечитать `DEPLOY_TROUBLESHOOTING.md` ПЕРЕД тем как давать команды**, а не давать их по памяти.

---

## 0.2 GitHub-зеркала: жив/мёртв (актуализируй после каждого инцидента)

С Timeweb VPS прямой `github.com` НЕ работает (RKN/egress). Используем URL-prefix зеркала: `<MIRROR>/https://github.com/<owner>/<repo>.git`.

| Зеркало | Статус (последняя проверка) | Заметки |
|---|---|---|
| `https://gh.idayer.com` | ✅ **2026-05-25 14:08** 200 OK 0.42с | Текущий known-good. Передавай через `GH_PROXY=https://gh.idayer.com` в `update.sh`. |
| `https://gh-proxy.com` | ❌ 2026-05-25 14:00 timeout 270с | Раньше дефолт. Регулярно уходит в техработы / DDoS. |
| `https://hub.gitmirror.com` | ❌ 2026-05-25 DNS resolve fail | Не резолвится с VPS. |
| `https://mirror.ghproxy.com` | ❌ 2026-05-25 DNS resolve fail | Не резолвится с VPS. |
| `https://kkgithub.com` | ❌ 2026-05-25 timeout 8с | |
| `https://gh.api.99988866.xyz` | ❌ 2026-05-25 SSL handshake failure | Срок сертификата истёк/SNI? |
| `https://ghproxy.org` | ❌ 2026-05-25 не git-mirror | Сейчас редиректит на маркетинг (домен на продаже). НЕ путать с `gh-proxy.com`. |
| `https://ghp.ci` | ❌ 2026-05-25 DNS resolve fail | |
| `https://ghfast.top` | ❌ 2026-05-25 timeout 8с | |

### Как пробить «жив или нет» зеркало одной командой

```bash
REPO=ReStartlex/fp_bot
BASE=https://gh.idayer.com   # или другой кандидат
curl -sS --max-time 8 -o /tmp/probe -w "%{http_code} %{time_total}s\n" \
  "${BASE}/https://github.com/${REPO}/info/refs?service=git-upload-pack"
head -c 30 /tmp/probe
```

Признак рабочего mirror:
- HTTP **200**
- Первые байты ответа: `001e# service=git-upload-pack` (git smart-HTTP сигнатура).

Если ничего не работает — массовая проверка кандидатов одной командой:
см. §6.1.

---

## 0. Контекст в одном экране

- Прод: `/opt/funpay-ns-bot` на Timeweb VPS (Ubuntu 24.04). Пользователь `bot`.
- SSH с локалки **режется провайдером**. Команды выполняются в
  **веб-консоли Timeweb** (от root).
- VPS-egress к `github.com` и часто к `gh-proxy.com` нестабилен / блокируется.
- В `.env` лежит SOCKS5-прокси (`166.88.218.111:62947`,
  `iRt8qjaa:Wdk3Gycf`) — основное «оружие» для обхода. См. п. 1.
- Текущий стабильный SHA для деплоя: `71426518cd038d57f647afcb5d37b90a606d55d4`
  (далее `7142651`).

---

## 0.4 Включение Phase 1 (TG-shop) на VPS

> Эта секция нужна **только при ПЕРВОМ** деплое после добавления shop-кода.
> Дальше — обычный TL;DR деплой через `update.sh`.

**Чек-лист для включения shop'а в проде:**

1. **Создать shop-бота через @BotFather:**
   - `/newbot` → задать имя (например, *MyShop Bot*) и username (например, `myshop_cards_bot`).
   - Сохранить токен в безопасное место.
   - (Опционально) `/setdescription`, `/setabouttext`, `/setuserpic`.

2. **Задеплоить новый код по обычному TL;DR** (см. выше) — без `SHOP_ENABLED=true`
   код безопасен: shop-бот не стартует, остальное работает как раньше.

3. **Добавить переменные в `/opt/funpay-ns-bot/.env`:**

   ```bash
   # на VPS, в веб-консоли Timeweb
   sudo nano /opt/funpay-ns-bot/.env
   # добавить в конец:
   SHOP_ENABLED=true
   SHOP_TELEGRAM_BOT_TOKEN=<твой_токен_от_BotFather>
   SHOP_MARKUP_PERCENT=8
   SHOP_REFERRAL_PERCENT=1
   SHOP_CATALOG_REFRESH_SECONDS=90
   ```

4. **Рестарт сервиса:**

   ```bash
   sudo systemctl restart funpay-ns-bot
   sudo journalctl -u funpay-ns-bot -n 50 --no-pager | grep -i shop
   ```

   Ожидаемо в логе: `Shop-бот @<username> стартовал (long-polling)`.
   В owner-чат прилетит: `🛒 Shop-бот @<username> запущен`.

5. **Проверить из Telegram:** `/start` в shop-боте → приветствие, регистрация
   в `shop_users`, `/balance` → 0₽, `/ref` → реф-ссылка.

**Откат:** выставить `SHOP_ENABLED=false` в `.env` и рестартануть — bridge-бот
продолжит работать, shop отключится.

---

## 1. Главные инсайты (2026-05-24 / 2026-05-25)

### 1.1 С Timeweb VPS прямой github НЕ работает, gh-proxy РАБОТАЕТ напрямую

Проверено на VPS 2026-05-25 (00:30 МСК):

```text
curl https://github.com/       → connection timed out (5s)
curl https://gh-proxy.com/     → 200 OK 0.57s    ✅
```

То есть **в дефолтной конфигурации `update.sh` (без env-переменных)** —
`fetch_code.sh` идёт `https://gh-proxy.com/https://github.com/...` —
с VPS он работает напрямую, прокси для git не нужен. Это **первая линия
обороны**:

```bash
bash /opt/funpay-ns-bot/deploy/update.sh
```

### 1.2 SOCKS5 из `.env` с Timeweb VPS может быть недоступен (egress-блок)

`166.88.218.111:62947` — это IP в датацентре ACE (`acedatacenter.com`).
Прокси сам по себе живой (с домашних провайдеров в РФ ответ за 1-2с,
проверено с Windows: `git ls-remote` через `socks5h://` вернул HEAD).
**Но с конкретного Timeweb VPS до этого IP не идёт даже TCP-handshake**
— `connection timed out` на уровне TCP. Возможные причины (не разбираем
по очереди, важно что факт): провайдер режет egress к подсетям популярных
прокси-провайдеров.

Кроме того, на VPS-овском `git 2.43 + libcurl 8.5.0` (Ubuntu 24.04)
наблюдалось, что `git config http.proxy=socks5h://...` libcurl **парсит
некорректно**: пытается подключиться к 166.88.218.111:62947 как к
HTTP-прокси через `CONNECT` метод и виснет на 134 секунды. На моей
Windows-машине тот же синтаксис работал — версии разные.

**Вывод**: рассчитывать на SOCKS5 для git с VPS — нельзя. Использовать
его можно только локально для диагностики.

### 1.3 «manual-tarball-via-proxy» в `BUILD_INFO` — это gh-proxy, не SOCKS5

Если в `BUILD_INFO` ты видишь `source=manual-tarball-via-proxy`,
то проксей был **gh-proxy.com** (его tarball-эндпоинт),
`https://gh-proxy.com/https://github.com/.../archive/<sha>.tar.gz`.
Этот путь — рабочая fallback-стратегия при любых проблемах с git
smart-http. См. ПЛАН D ниже.

---

## 2. Дерево решений

```
Нужно обновить VPS
│
├── [A] Бот СЕЙЧАС работает (даже если на старом коде)?
│   ├── Да  → идём ПЛАНОМ A (SOCKS5 как git proxy + pin). Безопасно.
│   └── Нет → идём ПЛАНОМ E (минимальный recovery). Только потом — A.
│
├── [B] У тебя есть свежий update.sh/fetch_code.sh на VPS (commit ≥ 6363321)?
│   ├── Да   → ПЛАН A (1 команда)
│   ├── Нет  → ПЛАН B (сначала обновить deploy/-скрипты курлом через прокси,
│   │                  потом ПЛАН A)
│   └── ХЗ   → выполняй DIAG-БЛОК 4.1, узнаешь
│
├── [C] gh-proxy.com и github.com совсем недоступны даже через SOCKS5?
│   └── → ПЛАН C: Cloudberry / Codeberg / другой mirror.
│
└── [D] Совсем всё плохо (прокси умерла, vpn нет)?
    └── → ПЛАН D: scp tarball (deploy/pack.ps1) через локальный обходной канал.
```

---

## 3. ПЛАН A — Рекомендуемый: SOCKS5 + PIN_SHA, один заход

> Подходит, если на VPS уже есть свежий `deploy/update.sh` (commit ≥ 6363321).

### A.0 (опционально, но полезно) — узнать что сейчас на VPS

```bash
cd /opt/funpay-ns-bot
cat BUILD_INFO 2>/dev/null || echo "(BUILD_INFO нет)"
cat .deploy_pin 2>/dev/null || echo "(deploy_pin нет — деплоится origin/main)"
grep -m1 'GIT_HTTP_PROXY' deploy/fetch_code.sh \
   && echo "OK: fetch_code.sh умеет GIT_HTTP_PROXY (>=6363321)" \
   || echo "СТАРАЯ ВЕРСИЯ — иди ПЛАНОМ B"
systemctl is-active funpay-ns-bot funpay-ns-api
```

### A.1 — Поставить pin на стабильный коммит

```bash
echo "71426518cd038d57f647afcb5d37b90a606d55d4" \
  | sudo tee /opt/funpay-ns-bot/.deploy_pin
chown bot:bot /opt/funpay-ns-bot/.deploy_pin
```

После этого ЛЮБОЙ будущий `update.sh` (даже без env) будет упираться в
этот SHA, пока ты не удалишь файл `.deploy_pin`.

### A.2 — Запустить обновление через SOCKS5

```bash
GIT_HTTP_PROXY='socks5h://iRt8qjaa:Wdk3Gycf@166.88.218.111:62947' \
  bash /opt/funpay-ns-bot/deploy/update.sh
```

Что произойдёт (`update.sh` спроектирован безопасно):

1. **Бэкап** `.env` + `data/bridge.db*` в `backups/<timestamp>/`.
2. **Fetch в staging** (`/opt/funpay-ns-bot.staging`) через SOCKS5. Бот
   продолжает работать на старом коде.
3. **Verify staging**: `compileall src/`, проверка `src/_version.py`,
   `src/main.py`, `requirements.txt`.
4. **Stop** `funpay-ns-bot` (и `funpay-ns-api`, если активен).
5. **Rsync** staging → production (с `--exclude .env data logs backups .venv .git`).
6. **pip install -r requirements.txt** в существующий `.venv`.
7. **Chown bot:bot** на всё, `chmod 600 .env`.
8. **systemctl start** + проверка `is-active`. На неуспех — рекомендация по откату.

### A.3 — Verify после деплоя

```bash
cat /opt/funpay-ns-bot/BUILD_INFO
journalctl -u funpay-ns-bot -n 50 --no-pager
systemctl status funpay-ns-bot --no-pager -l
```

В Telegram должно прийти `🟢 Бот запущен` (после `7142651` это
`ℹ️ Бот запущен ✅`, потому что коммит a28fc46 с новым «зелёным» сообщением
закрыт pin'ом).

---

## 4. Диагностика (когда непонятно где косяк)

### 4.1 — Проверить, что SOCKS5 работает с VPS

```bash
# curl напрямую через SOCKS5
curl --proxy 'socks5h://iRt8qjaa:Wdk3Gycf@166.88.218.111:62947' \
     -fsSL -o /dev/null -w "%{http_code} %{time_total}s\n" \
     https://github.com/

# git через SOCKS5
git -c http.proxy='socks5h://iRt8qjaa:Wdk3Gycf@166.88.218.111:62947' \
    ls-remote https://github.com/ReStartlex/fp_bot.git HEAD
```

Если оба возвращают данные за < 5с — прокси жив, причина не в нём.

### 4.2 — Полная матрица (если есть рабочий venv)

```bash
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.check_proxy \
    --full-matrix --timeout 15
```

Покажет таблицу `endpoint × profile` (direct / telegram / git_http) с
external-ip каждого профиля. Если `direct` мёртв, а `telegram` зелёный —
значит сетки/файрвол VPS блокируют прямой egress.

### 4.3 — Telegram-уведомления (notifier мёртв, меню живо)

Если **меню /menu отвечает**, а **уведомления «🟢 Бот запущен», «✅ Заказ
выполнен» не приходят** — почти наверняка `TelegramNotifier` затыкается
в прокси из `.env`, тогда как aiogram-бот идёт напрямую без прокси.

```bash
APP=/opt/funpay-ns-bot

# A) что в .env (маскируем пароль)
grep -iE '^(TELEGRAM_PROXY|TELEGRAM_ENABLED|TELEGRAM_CHAT_ID)' "$APP/.env" \
    | sed 's/PASSWORD=.*/PASSWORD=***/'

# B) логи notifier
journalctl -u funpay-ns-bot --since "15 minutes ago" --no-pager \
    | grep -iE 'telegram|sendmessage|прокси|proxy' | tail -40

# C) прямой тест API без прокси
TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$APP/.env" | cut -d= -f2- | tr -d '"')
curl -sS --max-time 8 "https://api.telegram.org/bot${TOKEN}/getMe" | head -c 200
echo

# D) тест через прокси из .env
PROXY_HOST=$(grep -E '^TELEGRAM_PROXY_HOST=' "$APP/.env" | cut -d= -f2- | tr -d '"')
PROXY_PORT=$(grep -E '^TELEGRAM_PROXY_PORT=' "$APP/.env" | cut -d= -f2- | tr -d '"')
PROXY_USER=$(grep -E '^TELEGRAM_PROXY_USERNAME=' "$APP/.env" | cut -d= -f2- | tr -d '"')
PROXY_PASS=$(grep -E '^TELEGRAM_PROXY_PASSWORD=' "$APP/.env" | cut -d= -f2- | tr -d '"')
curl -sS --max-time 8 \
    --socks5-hostname "${PROXY_USER}:${PROXY_PASS}@${PROXY_HOST}:${PROXY_PORT}" \
    "https://api.telegram.org/bot${TOKEN}/getMe" | head -c 200
```

**Интерпретация**: C дал JSON `{"ok":true,...}` за <1с → API напрямую
доступен; D вернул timeout/connection error → прокси мёртв. **Фикс**:
закомментировать `TELEGRAM_PROXY_*` в `.env`, перезапустить сервис:

```bash
sed -i 's/^\(TELEGRAM_PROXY_[A-Z_]*=\)/#\1/' /opt/funpay-ns-bot/.env
systemctl restart funpay-ns-bot
journalctl -u funpay-ns-bot --since "30 seconds ago" --no-pager | grep -i telegram
```

В чат должно прийти «🟢 Бот запущен».

### 4.4 — Лог последнего апдейта

```bash
# Если update.sh бежал из консоли, его вывод где-то у тебя.
# Сервис лог:
journalctl -u funpay-ns-bot -n 200 --no-pager

# Staging-папка может остаться, посмотри что в ней:
ls -lah /opt/funpay-ns-bot.staging/ 2>/dev/null
cat /opt/funpay-ns-bot.staging/BUILD_INFO 2>/dev/null
```

---

## 5. ПЛАН B — На VPS старый update.sh (без поддержки GIT_HTTP_PROXY)

> Признак: команда `grep GIT_HTTP_PROXY deploy/fetch_code.sh` пуста.

Тогда мы **вручную** подменим `deploy/` свежими версиями (через curl + SOCKS5),
а потом запустим обновление как обычно.

### B.1 — Скачать свежие скрипты ровно из стабильного коммита

```bash
PIN=71426518cd038d57f647afcb5d37b90a606d55d4
PROXY='socks5h://iRt8qjaa:Wdk3Gycf@166.88.218.111:62947'

for f in update.sh fetch_code.sh runtime.py; do
    curl --proxy "$PROXY" -fsSL \
      "https://raw.githubusercontent.com/ReStartlex/fp_bot/${PIN}/deploy/${f}" \
      -o "/tmp/${f}"
done

ls -lh /tmp/{update.sh,fetch_code.sh,runtime.py}
head -5 /tmp/update.sh   # должен начинаться с #!/usr/bin/env bash
```

### B.2 — Подменить в `/opt/funpay-ns-bot/deploy/`

```bash
APP=/opt/funpay-ns-bot
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p "$APP/backups/deploy-scripts-$ts"
cp "$APP/deploy/update.sh"     "$APP/backups/deploy-scripts-$ts/" 2>/dev/null
cp "$APP/deploy/fetch_code.sh" "$APP/backups/deploy-scripts-$ts/" 2>/dev/null
cp "$APP/deploy/runtime.py"    "$APP/backups/deploy-scripts-$ts/" 2>/dev/null

install -m 0755 -o bot -g bot /tmp/update.sh     "$APP/deploy/update.sh"
install -m 0755 -o bot -g bot /tmp/fetch_code.sh "$APP/deploy/fetch_code.sh"
install -m 0644 -o bot -g bot /tmp/runtime.py    "$APP/deploy/runtime.py"
```

### B.3 — Поставить pin и запустить ПЛАН A.2

```bash
echo "$PIN" | sudo tee "$APP/.deploy_pin"
chown bot:bot "$APP/.deploy_pin"

GIT_HTTP_PROXY="$PROXY" bash "$APP/deploy/update.sh"
```

---

## 6.1 — Массовая проверка GitHub-зеркал (если основное упало)

> Если `update.sh` упал на fetch, и в логе видно `Failed to connect to gh-proxy.com` (или другого зеркала) — запусти этот блок. Он за ~60 секунд найдёт живой mirror и подскажет какую `GH_PROXY=...` env-переменную передать в `update.sh`.

```bash
REPO=ReStartlex/fp_bot
echo "=== Поиск живого GitHub-зеркала ==="
for base in \
  https://gh.idayer.com \
  https://gh-proxy.com \
  https://hub.gitmirror.com \
  https://mirror.ghproxy.com \
  https://kkgithub.com \
  https://ghp.ci \
  https://gh-proxy.lyln.us.kg \
  https://github.moeyy.xyz \
  https://gh.api.99988866.xyz \
  https://ghfast.top; do
    url="${base}/https://github.com/${REPO}/info/refs?service=git-upload-pack"
    out=$(curl -sS --max-time 8 -o /tmp/probe.bin -w "%{http_code} %{time_total}s" "$url" 2>&1)
    sig=$(head -c 30 /tmp/probe.bin 2>/dev/null | tr -dc '[:print:]' | head -c 25)
    if echo "$sig" | grep -q "git-upload-pack"; then
      echo "  ✅ LIVE: $base  ($out)"
    else
      echo "     dead: $base  ($out)"
    fi
done
```

Возьми первый «✅ LIVE» — это твоё `GH_PROXY` для деплоя:

```bash
sudo GH_PROXY=<living_mirror_url> bash /opt/funpay-ns-bot/deploy/update.sh
```

После успешного деплоя — **обнови §0.2** в этом документе (там таблица «жив/мёртв» с датой).

---

## 6. ПЛАН C — Прокси не работает / GitHub мёртв даже через прокси

### C.1 — Codeberg mirror

`fetch_code.sh` поддерживает `CODEBERG_URL`. Если ты сделаешь
зеркало нашего репо на codeberg.org (read-only mirror через GitHub
Actions, или просто `git push --mirror`), то:

```bash
CODEBERG_URL='https://codeberg.org/<owner>/fp_bot.git' \
PIN_SHA=71426518cd038d57f647afcb5d37b90a606d55d4 \
  bash /opt/funpay-ns-bot/deploy/update.sh
```

⚠️ На дату 2026-05-24 mirror НЕ настроен. Это «план Б на завтра».

### C.2 — Прямой `git fetch` через любой другой работающий прокси

```bash
git config --global http.proxy  'http://USER:PASS@HOST:PORT'
git config --global https.proxy 'http://USER:PASS@HOST:PORT'

cd /opt/funpay-ns-bot
git fetch origin 71426518cd038d57f647afcb5d37b90a606d55d4
git reset --hard 71426518cd038d57f647afcb5d37b90a606d55d4

# В конце ОБЯЗАТЕЛЬНО снять прокси:
git config --global --unset http.proxy
git config --global --unset https.proxy
```

Затем вручную пройти шаги 6-8 из `update.sh`:

```bash
chown -R bot:bot /opt/funpay-ns-bot
chmod 600 /opt/funpay-ns-bot/.env
/opt/funpay-ns-bot/.venv/bin/pip install -r /opt/funpay-ns-bot/requirements.txt

# рефреш systemd units (на случай если service-файл изменился)
cp /opt/funpay-ns-bot/deploy/funpay-ns-bot.service /etc/systemd/system/
cp /opt/funpay-ns-bot/deploy/funpay-ns-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart funpay-ns-bot funpay-ns-api
```

---

## 7. ПЛАН D — Tarball через scp (последний рубеж)

> Когда вообще никакая сеть с VPS до внешнего git не работает.

### D.1 — Локально (Windows)

```powershell
cd D:\money
git checkout 71426518cd038d57f647afcb5d37b90a606d55d4  # на отдельной ветке
.\deploy\pack.ps1                                       # создаст D:\money\app.zip
```

### D.2 — Доставить на VPS

Способы по убыванию удобства:

1. **Через Cloudflare Tunnel** — если уже настроен туннель к Web API,
   можно временно поднять отдельный туннель к sftp.
2. **Через локальный обходной канал** (другой провайдер / мобильная сеть):
   ```bash
   scp app.zip root@VPS_IP:/opt/funpay-ns-bot/
   ```
3. **Через файловый менеджер Timeweb** (в их веб-панели VPS есть File Manager).

### D.3 — На VPS

```bash
cd /opt/funpay-ns-bot
bash deploy/install_app.sh
# install_app.sh: unzip app.zip → pip install → chown → daemon-reload
systemctl restart funpay-ns-bot funpay-ns-api
```

---

## 8. ПЛАН E — Минимальный recovery (бот лежит)

> Когда `systemctl status funpay-ns-bot` показывает `failed` и логи
> ругаются на код / отсутствующий модуль.

### E.1 — Откат из последнего бэкапа

```bash
ls /opt/funpay-ns-bot/backups/
# выбери самый свежий, например 20260524-141200/

BK=/opt/funpay-ns-bot/backups/20260524-141200
systemctl stop funpay-ns-bot funpay-ns-api

# Восстанавливаем .env и БД
cp "$BK/.env"             /opt/funpay-ns-bot/.env
cp "$BK/data/bridge.db"   /opt/funpay-ns-bot/data/bridge.db 2>/dev/null
cp "$BK/data/bridge.db-wal" /opt/funpay-ns-bot/data/bridge.db-wal 2>/dev/null
cp "$BK/data/bridge.db-shm" /opt/funpay-ns-bot/data/bridge.db-shm 2>/dev/null

# Код: либо берём предыдущий коммит из git reflog, либо ставим pin
echo "71426518cd038d57f647afcb5d37b90a606d55d4" > /opt/funpay-ns-bot/.deploy_pin

chown -R bot:bot /opt/funpay-ns-bot
chmod 600 /opt/funpay-ns-bot/.env

# Когда сеть восстановится:
GIT_HTTP_PROXY='socks5h://iRt8qjaa:Wdk3Gycf@166.88.218.111:62947' \
  bash /opt/funpay-ns-bot/deploy/update.sh
```

### E.2 — Если venv тоже сломан

```bash
rm -rf /opt/funpay-ns-bot/.venv
python3.12 -m venv /opt/funpay-ns-bot/.venv
/opt/funpay-ns-bot/.venv/bin/pip install --upgrade pip wheel
/opt/funpay-ns-bot/.venv/bin/pip install -r /opt/funpay-ns-bot/requirements.txt
chown -R bot:bot /opt/funpay-ns-bot
```

---

## 9. Полезные one-liners (cheat sheet)

```bash
# Текущая версия кода на VPS
cat /opt/funpay-ns-bot/BUILD_INFO

# Какой коммит зафиксирован pin'ом
cat /opt/funpay-ns-bot/.deploy_pin 2>/dev/null || echo "(no pin)"

# Какие бэкапы есть
ls -lt /opt/funpay-ns-bot/backups/ | head -20

# Удалить pin (вернуться к деплоям с origin/main):
rm /opt/funpay-ns-bot/.deploy_pin

# Логи последних 5 минут
journalctl -u funpay-ns-bot --since "5 minutes ago" --no-pager

# Понять, что бот вообще делает прямо сейчас (нагрузка)
journalctl -u funpay-ns-bot -f --no-pager

# Перепроверка proxy ИЗ-ПОД сервиса (с тем же .env, что и бот)
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.check_proxy \
    --full-matrix --timeout 15

# Срочный shutdown
systemctl stop funpay-ns-bot funpay-ns-api
```

---

## 10. Журнал «что пробовал»

> Заполняй после каждой попытки. Дата — UTC+3 (МСК).

### 2026-05-23 — инцидент с `gh-proxy.com`
- **Симптом**: `update.sh` остановил сервис, потом `git fetch` через
  `gh-proxy.com` завис → прод лежал ~30 мин.
- **Решение**: переписали `update.sh` на fetch-then-verify-then-stop
  (коммит `304441d`). Сейчас сетевой сбой → `exit 1` без остановки бота.

### 2026-05-23 — введён GIT_HTTP_PROXY
- **Коммит**: `6363321 feat(deploy): safer updates with backups, commit pin and proxy fallback`
- Появилась поддержка `PIN_SHA`/`.deploy_pin` и `GIT_HTTP_PROXY` в
  `fetch_code.sh`.

### 2026-05-24 — open вопрос: коннект с VPS к GitHub
- **Симптом**: с VPS не получается ни git fetch, ни curl на github.com.
  Последняя попытка `update.sh` упала на fetch.
- **Гипотеза**: Timeweb VPS режет egress / RKN-блокировки / нестабильность
  `gh-proxy.com`.
- **Проверка локально (Windows D:\money)**:
  - `python -m src.tools.check_proxy --full-matrix` —
    профиль `telegram` (SOCKS5 из `.env`) зелёный по всем endpoint'ам,
    включая github.com и gh-proxy.com.
  - `curl --proxy socks5h://...@166.88.218.111:62947 https://github.com/...` —
    реальный файл `src/_version.py` получен.
  - `git -c http.proxy=socks5h://... ls-remote https://github.com/...` —
    вернул `a28fc4681b6a…` (HEAD origin/main).
- **Гипотеза**: использовать SOCKS5 из `.env` как `GIT_HTTP_PROXY`.

### 2026-05-25 — диагностика на VPS, гипотеза с SOCKS5 НЕ подтвердилась
- Запущена диагностика A/B/C/D/E на VPS (Ubuntu 24.04, git 2.43,
  curl 8.5.0):
  - все 4 варианта git (`ALL_PROXY`, `HTTPS_PROXY`, `socks5://`,
    `gh-proxy via ALL_PROXY`) → `Terminated` (timeout 20s).
  - `curl --proxy socks5h://...` через прокси → `connection timed out 12s`.
  - **`tcp до 166.88.218.111:62947` → TIMEOUT**: VPS не открывает
    даже TCP-соединение с прокси.
  - DNS работает (`getent hosts` всё резолвит).
  - **`curl https://github.com/` напрямую → timed out 5s** (как и
    ожидалось).
  - **`curl https://gh-proxy.com/` напрямую → 200 OK за 0.57s** ✅
- **Вывод**: с этого VPS до 166.88.218.111:62947 маршрут закрыт
  (Timeweb egress либо сам прокси-провайдер режет). Зато gh-proxy.com
  отвечает прямо за 0.57с без всякого прокси.
- **Решение**: запустить `update.sh` без env-переменных (default использует
  gh-proxy.com). Если git smart-http через gh-proxy не сработает —
  fallback на tarball через gh-proxy (тот же канал, что у тебя уже
  работал, см. `source=manual-tarball-via-proxy` в `BUILD_INFO`).
- **На VPS pin успешно обновлён**: `.deploy_pin = 7142651`.
- **Альтернативный прокси на будущее** (на случай если потребуется):
  `http://modeler_lLeftL:0quI4pXS96Wv@172.235.32.100:10854`
  (НЕ тестировался с VPS).
- **Статус**: команда выкатки готова — см. п. 1.1 и ПЛАН A. Ожидание
  прогона на VPS.

### 2026-05-25 — два мёртвых прокси в env'е + поломка fetch при staging

- **Симптом 1**: `update.sh` падал на fetch с `Could not resolve host` /
  `connection timed out` при том, что прямой `curl gh-proxy.com` работал.
- **Найдено**: в локальном `/opt/funpay-ns-bot/.git/config` сидел старый
  HTTP-прокси `http://modeler_lLeftL:...@172.235.32.100:10854` (не из `.env`,
  оставлен предыдущей попыткой ручного фикса). Плюс в env'е shell-сессии
  висели `HTTP_PROXY=...:62946` и `ALL_PROXY=socks5://...:62946` (опечатка
  в порту: 62946 вместо 62947, прокси с таким портом не существует).
- **Решение в коде** (`fetch_code.sh` + `update.sh`):
  - Очищать `local` git-конфиг прокси на старте каждого запуска.
  - НЕ писать `git config --global`. Использовать `git -c http.proxy=...`
    per-command, явно подставлять пустой proxy если `GIT_HTTP_PROXY` не задан
    (перебивает env-vars unrelated).
  - `.deploy_pin` резолвить из `PROD_APP_DIR`, а не из `APP_DIR` (который
    в момент fetch'а указывает на staging-каталог).
- **Симптом 2**: чат-handler слал `greeting_pre_purchase` **два раза подряд**
  одному покупателю на одно сообщение.
- **Корень**: watcher имеет два канала (listen без `message_id` → text-key
  дедуп; poll с `message_id` → id-key). Ключи разные → одно сообщение
  диспатчится в handler ДВАЖДЫ. В `_maybe_greet` старый код делал
  `SELECT chat_state → check greeted_at → UPDATE → commit → send`, что
  создавало classic check-then-act race. Параллельные таски видели
  `greeted_at=None` и оба слали приветствие.
- **Решение в коде**: `mark_greeted_if_due` делает атомарный
  conditional UPDATE (`WHERE greeted_at IS NULL OR greeted_at < cutoff`)
  и возвращает `rowcount > 0`. Только выигравший гонку task шлёт
  сообщение в FunPay. Дополнительно `get_or_create_chat_state` теперь
  использует `INSERT ... ON CONFLICT DO NOTHING`, чтобы параллельные
  INSERT для нового чата не падали с UNIQUE constraint.
- **Тесты**: `tests/test_greeting_race.py` (race + cooldown + per-chat).

### 2026-05-25 — Telegram-уведомления отвалились, меню работает (open)

- **Симптом**: бот отвечает на `/menu` в Telegram, **но** уведомления
  «🟢 Бот запущен» / «🔴 Бот остановлен» / «✅ Заказ выполнен» **не
  приходят**.
- **Корневая гипотеза**: `TelegramBot` (aiogram, для приёма команд)
  создаётся через `Bot(token=...)` **без proxy** → идёт напрямую к
  api.telegram.org и работает. `TelegramNotifier` (httpx, для исходящих
  алертов) подхватывает `TELEGRAM_PROXY_*` из `.env` → если SOCKS5
  `166.88.218.111:62947` с VPS недоступен (egress блок, уже
  подтверждался), все алерты тихо фейлятся в `Telegram sendMessage failed`.
- **Диагностика**: см. блок A/B/C/D/E ниже («Telegram-уведомления»).
- **Фикс**: если direct API доступен с VPS (а он доступен — aiogram-бот
  работает), убрать `TELEGRAM_PROXY_*` из `.env` **полностью**, чтобы
  notifier пошёл напрямую. Перезапустить сервис.

### 2026-05-25 — ассистент дал команду без `bash`, VPS ответил `command not found`

- **Симптом** (видно на скрине пользователя):
  ```
  sudo: /opt/funpay-ns-bot/deploy/update.sh: command not found
  ```
- **Корень**: ассистент дал блок:
  ```bash
  sudo GH_PROXY=https://gh.idayer.com PROD_APP_DIR=/opt/funpay-ns-bot \
        /opt/funpay-ns-bot/deploy/update.sh
  ```
  Две ошибки:
  1. `update.sh` в репозитории **не имеет executable-бита** (это
     намеренно, чтобы rsync не нёс +x как часть payload'а). Запуск
     `sudo $APP/deploy/update.sh` без `bash` → shell ищет executable
     по пути, не находит → `command not found`.
  2. Передан лишний `PROD_APP_DIR=` — переменная не используется
     `update.sh` (он сам определяет prod-каталог), только засоряет env.
  Плюс ассистент **не сбросил `.deploy_pin`** от прошлой сессии и
  **не очистил staging**, что в более «удачном» сценарии привело бы
  к деплою старого SHA молча.
- **Правильная команда** (зафиксирована в §0.1 и TL;DR, всегда):
  ```bash
  APP=/opt/funpay-ns-bot
  sudo rm -f "$APP/.deploy_pin"         # ← если хотим свежий main, не пин
  sudo rm -rf /opt/funpay-ns-bot.staging
  sudo GH_PROXY=https://gh.idayer.com bash "$APP/deploy/update.sh"
  cat "$APP/BUILD_INFO"
  ```
- **Защита от рецидива**: §0.1 теперь содержит грабли «без `bash`»,
  «забытый `.deploy_pin`», «забытый staging», «лишний `PROD_APP_DIR`».
  Перед любым деплоем ассистент **обязан перечитать §0.1 + TL;DR**.

### 2026-05-25 — ассистент дал кривые команды деплоя, консоль зависла

- **Симптом**: пользователь скопировал блок команд из чата, в веб-консоли
  Timeweb команды зависли (висели больше 10 часов до возврата).
- **Корень**: ассистент НЕ перечитал `DEPLOY_TROUBLESHOOTING.md` перед
  тем как давать команды, и **по памяти** написал:
  ```bash
  sudo -u bot git -c http.proxy=https://gh-proxy.com fetch --prune
  ```
  Это две ошибки сразу:
  1. `gh-proxy.com` — это **URL-префикс зеркала**, а не HTTP-прокси.
     Git трактует `http.proxy` как proxy-сервер, шлёт ему `CONNECT
     github.com:443 HTTP/1.1` → gh-proxy не понимает → timeout 120с+.
  2. Команда `git fetch --prune` руками вообще лишняя — `update.sh`
     сам делает fetch в staging-папку.
- **Правильная команда** (из §1.1 этого документа, существует с 2026-05-25):
  ```bash
  APP=/opt/funpay-ns-bot
  echo "<полный_SHA>" | sudo tee "$APP/.deploy_pin"
  sudo chown bot:bot "$APP/.deploy_pin"
  sudo bash "$APP/deploy/update.sh"
  ```
  Без env-переменных. Default path в `fetch_code.sh` использует
  `gh-proxy.com` как URL-префикс корректно.
- **Что добавлено в документ**: новый блок «⚡ TL;DR» в самом верху +
  таблица «Грабли, которые я повторяю чаще всего» (§0.1). Теперь
  любой следующий чат сначала упрётся в эти блоки и не повторит
  ошибку.
- **Мета-урок для ассистента**: в начале каждой новой сессии **первая
  файловая операция** должна быть `Read DEPLOY_TROUBLESHOOTING.md` —
  до того как давать ЛЮБЫЕ команды для VPS.

### 2026-05-25 — gh-proxy.com упал, найдено новое зеркало gh.idayer.com

- **Симптом**: `update.sh` упал с `Failed to connect to gh-proxy.com port
  443 after 270587 ms`. Десять минут спустя — gh-proxy всё ещё лежит.
- **Диагностика** (см. §6.1 — теперь это готовый блок):
  - `gh-proxy.com` timeout 270с
  - `github.com` напрямую — timeout (RKN/Timeweb egress, как всегда)
  - DNS на VPS многие популярные mirror'ы НЕ резолвит:
    `hub.gitmirror.com`, `mirror.ghproxy.com`, `gh-proxy.lyln.us.kg`,
    `github.moeyy.xyz`, `ghp.ci` — все `Could not resolve host`.
  - `gh.api.99988866.xyz` — SSL handshake failure.
  - `ghproxy.org` — больше не git-mirror, домен на продаже, редиректит
    на маркетинг.
  - `ghfast.top` — timeout 8с.
  - **`gh.idayer.com` → 200 OK 0.42с с правильной git smart-HTTP
    сигнатурой `001e# service=git-upload-pack`**. ✅
- **Решение**:
  ```bash
  sudo rm -rf /opt/funpay-ns-bot.staging
  sudo GH_PROXY=https://gh.idayer.com bash /opt/funpay-ns-bot/deploy/update.sh
  ```
- **Что добавлено в документ**:
  - §0.2 «GitHub-зеркала: жив/мёртв» — таблица актуальных статусов.
    Обновлять после каждого деплоя.
  - §6.1 «Массовая проверка GitHub-зеркал» — готовый блок поиска
    живого mirror'а за 60 секунд.
  - TL;DR (`⚡` блок в шапке) теперь использует `gh.idayer.com` по
    умолчанию (вместо мёртвого `gh-proxy.com`).
- **Мета-урок**: GitHub-зеркала непостоянны. То что работало вчера,
  сегодня может умереть. **Всегда проверять §0.2** перед deploy-командой,
  а после успешного деплоя — обновлять таблицу с новой датой.

### <добавь сюда следующую попытку>

---

## 11. Если ничего не помогает — что собрать в чат

Чтобы следующий чат с ассистентом мог тебе помочь быстрее, скопируй
вывод этих команд в чат:

```bash
echo "=== uname ==="; uname -a
echo "=== current code ==="; cat /opt/funpay-ns-bot/BUILD_INFO 2>/dev/null
echo "=== pin ==="; cat /opt/funpay-ns-bot/.deploy_pin 2>/dev/null || echo none
echo "=== fetch_code.sh has GIT_HTTP_PROXY support? ==="
grep -m1 GIT_HTTP_PROXY /opt/funpay-ns-bot/deploy/fetch_code.sh \
   && echo yes || echo no
echo "=== systemd ==="; systemctl is-active funpay-ns-bot funpay-ns-api
echo "=== last log lines ==="
journalctl -u funpay-ns-bot -n 50 --no-pager
echo "=== curl direct github ==="
curl -fsSL -o /dev/null -w "%{http_code} %{time_total}s\n" \
    --max-time 10 https://github.com/ 2>&1 || echo "FAIL"
echo "=== curl github via SOCKS5 ==="
curl --proxy 'socks5h://iRt8qjaa:Wdk3Gycf@166.88.218.111:62947' \
    -fsSL -o /dev/null -w "%{http_code} %{time_total}s\n" \
    --max-time 10 https://github.com/ 2>&1 || echo "FAIL"
echo "=== git ls-remote via SOCKS5 ==="
git -c http.proxy='socks5h://iRt8qjaa:Wdk3Gycf@166.88.218.111:62947' \
    ls-remote https://github.com/ReStartlex/fp_bot.git HEAD 2>&1 || echo "FAIL"
```
