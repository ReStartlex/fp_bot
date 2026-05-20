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
напрямую. Для публичного сайта позже ставим reverse proxy с HTTPS.

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

Логи:

```bash
journalctl -u funpay-ns-api -f
```

## Обновление кода

```bash
bash /opt/funpay-ns-bot/deploy/update.sh
```

Скрипт скачивает свежий tarball через `gh-proxy.com` (обход блокировки
GitHub с Timeweb), обновляет зависимости, перезапускает systemd-сервис.
Если `funpay-ns-api` уже был включён или запущен, `update.sh` также аккуратно
остановит и поднимет API после обновления.

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
