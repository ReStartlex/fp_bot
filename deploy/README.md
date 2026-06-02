# Развёртывание на VPS

Сервер — Timeweb VPS, выход в интернет к `ns.gifts` через whitelisted IP.
SSH с локальной машины может быть недоступен из-за блокировок провайдера;
все шаги выполняются в веб-консоли Timeweb или через Tailscale (опционально).

## Первичная установка

В веб-консоли как root:

```bash
apt update -qq && apt install -y -qq git
git clone https://github.com/ReStartlex/fp_bot.git /opt/funpay-ns-bot
bash /opt/funpay-ns-bot/deploy/bootstrap.sh
```

`bootstrap.sh` ставит python, venv, зависимости, создаёт пользователя `bot`,
настраивает `ufw` (22/tcp, 443/tcp) и `fail2ban`, регистрирует systemd-units
для бота и отдельного Web API.

### `.env` на сервере

Скопируй содержимое локального `.env` в файл на сервере, через heredoc:

```bash
cat > /opt/funpay-ns-bot/.env <<'EOF'
<сюда — содержимое локального .env как есть>
EOF
chown bot:bot /opt/funpay-ns-bot/.env
chmod 600 /opt/funpay-ns-bot/.env
```

Одинарные кавычки вокруг `EOF` обязательны, иначе `$` внутри пароля будут
интерпретированы как переменная.

### Проверки

```bash
cd /opt/funpay-ns-bot
sudo -u bot .venv/bin/python -m src.tools.check_ns
sudo -u bot .venv/bin/python -m src.tools.check_funpay
sudo -u bot .venv/bin/python -m src.tools.check_telegram
```

Если `check_funpay` падает с `UnauthorizedError`, см. раздел «Обновление
cookies FunPay» ниже.

## Запуск 24/7

```bash
systemctl daemon-reload
systemctl enable --now funpay-ns-bot
systemctl status funpay-ns-bot --no-pager
journalctl -u funpay-ns-bot -f
```

В Telegram должно прийти сообщение `Бот запущен ✅`. После этого писать боту
`/help` — он отвечает справкой.

## Web API для будущего сайта

API запускается отдельным сервисом `funpay-ns-api` и не влияет на основной
бот. По умолчанию он слушает только `127.0.0.1:8080`; не открывай порт наружу
напрямую.

В `.env` должны быть:

```bash
WEB_API_ENABLED=true
WEB_API_HOST=127.0.0.1
WEB_API_PORT=8080
WEB_API_TOKEN=<длинный_случайный_токен>
```

Запуск:

```bash
systemctl daemon-reload
systemctl enable --now funpay-ns-api
systemctl status funpay-ns-api --no-pager
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.check_web_api
```

Админка сайта встроена в API и доступна на `/`. Она не хранит токен на сервере:
браузер сохраняет `WEB_API_TOKEN` в localStorage и отправляет его как
`Authorization: Bearer ...`.

### Доступ без SSH через Cloudflare Tunnel

Если твой оператор режет SSH/прямые подключения к VPS, используй Cloudflare
Tunnel. Это безопаснее для текущей схемы: входящие порты на VPS не нужны,
`funpay-ns-api` остаётся на `127.0.0.1`, а наружу выходит только домен
Cloudflare.

1. В Cloudflare добавь домен и включи Zero Trust.
2. Zero Trust → Networks → Tunnels → Create tunnel → Cloudflared.
3. Создай Public Hostname, например `panel.example.com`.
4. В Service укажи:
   ```text
   http://127.0.0.1:8080
   ```
5. Скопируй tunnel token и на VPS выполни:
   ```bash
   cd /opt/funpay-ns-bot
   CLOUDFLARE_TUNNEL_TOKEN='<token_из_Cloudflare>' \
     bash deploy/install_cloudflare_tunnel.sh
   ```
6. Проверка:
   ```bash
   systemctl status cloudflared --no-pager -l
   journalctl -u cloudflared -f
   ```

После этого открывай `https://panel.example.com`, вставляй `WEB_API_TOKEN`
в админке и работай через сайт.

Логи:

```bash
journalctl -u funpay-ns-api -f
```

## Обновление кода

```bash
bash /opt/funpay-ns-bot/deploy/update.sh
```

Что делает `update.sh` (в порядке шагов):

1. **Бэкап** `.env` + `data/bridge.db` (с WAL/SHM) в
   `/opt/funpay-ns-bot/backups/<timestamp>/`. Хранит последние
   `BACKUP_KEEP` снапшотов (по умолчанию 10). Делается ДО остановки
   сервиса, чтобы БД была в консистентном состоянии.
2. **FETCH в STAGING** (`/opt/funpay-ns-bot.staging`). Сервис **продолжает работать**
   на старом коде. Тянет код через `gh-proxy.com` (или прямой git, см. ниже).
   Если fetch упал (сетевой сбой, мёртвый прокси) — `exit 1` БЕЗ остановки
   сервиса.
3. **VERIFY STAGING** (`python -m deploy.runtime verify-staging`): что есть
   `src/_version.py`, `src/main.py`, `requirements.txt`; что `_version.py`
   содержит маркеры `SHA`/`SUBJECT` (защита от случая, когда fetch вернул
   HTML страницу ошибки GitHub'а); что `compileall src/` проходит без
   SyntaxError. Если verify упал — `exit 1` БЕЗ остановки сервиса.
4. **STOP** `funpay-ns-bot` (и `funpay-ns-api`, если активен). Только теперь —
   новый код гарантированно валиден.
5. **RSYNC** staging → production, с exclude для `.env`, `data/`, `logs/`,
   `backups/`, `.venv/`, `.git/`, `.deploy_pin`. Чистка `__pycache__` на проде.
6. Прокатывает `pip install -r requirements.txt`.
7. Чинит права (`bot:bot`, `chmod 600` на `.env` и бэкапы).
8. Поднимает сервисы обратно, делает health-check, печатает версию.
9. Если сервис **не поднялся** — печатает короткую инструкцию по откату
   из последнего бэкапа.

Главное про этот порядок: **сервис никогда не остановлен, пока новый код не
проверен**. Это спасает от инцидента 23.05.2026, когда `update.sh` сначала
остановил сервис, потом git fetch завис на мёртвом прокси, и продакшн
остался лежать. Теперь любой сетевой сбой → `exit 1` без потери uptime.

Полезные переменные окружения для `update.sh` (все опциональные):

| Переменная | Что делает |
|---|---|
| `PIN_SHA` | Зафиксировать обновление на конкретный коммит. Защищает от случайного отката на «плохой» main. |
| `GIT_HTTP_PROXY` | HTTP-прокси для git напрямую на github.com (fallback, если `gh-proxy.com` на тех. работах). Применяется только если этот путь известно рабочий — иногда прокси пускает curl, но не git client. |
| `BACKUP_KEEP` | Сколько последних бэкапов держать (по умолчанию 10). |
| `STAGING_DIR` | Где собирать staging (по умолчанию `${APP_DIR}.staging`). |

Примеры:

```bash
# Стандартное обновление (как раньше).
bash /opt/funpay-ns-bot/deploy/update.sh

# Откатиться/обновиться на конкретный SHA (после инцидента в main).
PIN_SHA=708ed212aa150f6cc45471ff7bb735da1ef0d010 \
  bash /opt/funpay-ns-bot/deploy/update.sh

# Когда gh-proxy.com на тех. работах — идём через свой HTTP-прокси.
GIT_HTTP_PROXY='http://user:pass@proxy.example.com:8080' \
  bash /opt/funpay-ns-bot/deploy/update.sh
```

### Постоянный pin: файл `.deploy_pin`

Если хочется зафиксировать версию надолго (например, до полного аудита
новых коммитов после инцидента), вместо `PIN_SHA` в env удобнее
положить SHA в файл:

```bash
echo "708ed212aa150f6cc45471ff7bb735da1ef0d010" \
    | sudo -u bot tee /opt/funpay-ns-bot/.deploy_pin
```

После этого любой `update.sh` будет упираться в этот коммит, пока ты
не удалишь файл:

```bash
rm /opt/funpay-ns-bot/.deploy_pin
```

`PIN_SHA` через env имеет приоритет над файлом, чтобы можно было разово
прокатить новую версию без редактирования файла.

### Если сервис не поднялся: откат из бэкапа

`update.sh` сам напечатает шаги. Базовый сценарий:

```bash
systemctl stop funpay-ns-bot funpay-ns-api
cp /opt/funpay-ns-bot/backups/<timestamp>/.env /opt/funpay-ns-bot/.env
cp /opt/funpay-ns-bot/backups/<timestamp>/data/bridge.db \
   /opt/funpay-ns-bot/data/bridge.db
PIN_SHA=<sha_прошлой_рабочей_версии> bash /opt/funpay-ns-bot/deploy/update.sh
```

## Обновление cookies FunPay

`golden_key` и `phpsessid` живут пока ты не вышел и пока FunPay не
инвалидировал сессию (например после блокировки/разблокировки или после
смены IP). Если в логах `UnauthorizedError` — значения протухли.

1. Открой `funpay.com` в своём браузере, залогинься.
2. F12 → вкладка `Application` (Chrome/Edge) или `Storage` (Firefox) →
   `Cookies` → `https://funpay.com`.
3. Скопируй значения `golden_key` и `PHPSESSID`.
4. Положи в `.env` на сервере:

```bash
nano /opt/funpay-ns-bot/.env
# обнови:
# FUNPAY_GOLDEN_KEY=...
# FUNPAY_PHPSESSID=...
```

5. Перезапусти:

```bash
systemctl restart funpay-ns-bot
journalctl -u funpay-ns-bot -n 30 --no-pager
```

В Telegram должно прийти `FunPay подключён: id=..., username=...`.

## Tailscale (опционально)

После первичной установки можно поднять Tailscale — это вернёт нормальный
SSH мимо блокировок:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh
```

После авторизации в браузере и установки Tailscale на локальную машину
сервер доступен по короткому имени или внутреннему IP.

## Безопасность

- Смени пароль root в первый же заход: `passwd`.
- После настройки Tailscale можно отключить вход по паролю:
  ```bash
  sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  systemctl restart ssh
  ```
- `.env` должен быть `chmod 600` и принадлежать пользователю `bot`.
- Контроль: `ENABLE_REAL_ACTIONS=false` пока не закончил тесты.

## Откат

Если что-то сломалось после обновления:

```bash
systemctl stop funpay-ns-bot
cd /opt/funpay-ns-bot
git log --oneline -10                  # подсмотреть нужный коммит
# либо вручную поставить нужный tarball
systemctl start funpay-ns-bot
```
