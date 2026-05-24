# DEPLOY_TROUBLESHOOTING.md

> Дерево решений для обновления VPS, когда «git не качает».
> Журнал «что пробовал» — внизу. **Дополняй после каждой попытки**, чтобы
> следующий чат не наступал на те же грабли.

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
