# Deploy на VPS (через Git)

Так как у нас провайдер режет TCP к Timeweb (ping проходит, TCP — нет),
работаем через **Git + веб-консоль Timeweb**.

Сервер: Timeweb VPS (IP смотри в ЛК хостинга или в `.env`)
Репо: `https://github.com/ReStartlex/fp_bot.git`

---

## Одноразовая первичная установка

### 1. Локально: запушить проект на GitHub

```powershell
cd D:\money
git init -b main
git add .
git commit -m "Initial commit: F0 (NS client + structure)"
git remote add origin https://github.com/ReStartlex/fp_bot.git
git push -u origin main
```

Если git попросит логин/пароль — нужен **Personal Access Token** GitHub:
GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) →
Generate new token → дай ему scope `repo` → скопируй (показывается один раз).
Логин = твой github-логин, пароль = этот токен.

### 2. На сервере: открыть веб-консоль Timeweb → залогиниться как root → вставить:

```bash
apt update -qq && apt install -y -qq git
git clone https://github.com/ReStartlex/fp_bot.git /opt/funpay-ns-bot
bash /opt/funpay-ns-bot/deploy/bootstrap.sh
```

Скрипт развернёт всё за 3-5 минут. В конце напомнит создать `.env`.

### 3. На сервере: создать `.env` (одной командой через heredoc)

Скопируй из локального `D:\money\.env` своё содержимое и вставь между `'EOF'`:

```bash
cat > /opt/funpay-ns-bot/.env <<'EOF'
<сюда вставить ВСЁ содержимое локального .env как есть>
EOF
chown bot:bot /opt/funpay-ns-bot/.env
chmod 600 /opt/funpay-ns-bot/.env
```

⚠️ Важно: `'EOF'` именно в одинарных кавычках, чтобы шелл не пытался
интерпретировать `$` внутри пароля как переменную.

### 4. На сервере: проверить NS API

```bash
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.check_ns
```

Ожидаем: `Все проверки пройдены. NS API работает.`

---

## Обновление кода

**Локально:**

```powershell
git add .
git commit -m "..."
git push
```

**На сервере (одна команда):**

```bash
bash /opt/funpay-ns-bot/deploy/update.sh
```

Скрипт скачает свежий код через `gh-proxy`, обновит зависимости и
перезапустит systemd-сервис (если он включён).

---

## Запуск бота

### Telegram: первый запуск (определить chat_id)

1. В `.env` укажи `TELEGRAM_BOT_TOKEN`, оставь `TELEGRAM_CHAT_ID` пустым.
2. На сервере запусти discovery (он будет ждать твоего сообщения):

```bash
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.discover_chat_id
```

3. Открой Telegram, найди своего бота, напиши ему `/start`.
4. Скрипт распечатает твой `chat_id`. Скопируй в `.env`:

```bash
nano /opt/funpay-ns-bot/.env
# TELEGRAM_CHAT_ID=123456789
```

5. Проверь, что нотификации отправляются:

```bash
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.check_telegram
```

### Включить systemd-сервис (24/7)

```bash
systemctl enable --now funpay-ns-bot
systemctl status funpay-ns-bot
journalctl -u funpay-ns-bot -f
```

В Telegram придёт сообщение «Бот запущен ✅». В чате с ботом командой
`/status` можно увидеть состояние, `/help` — список команд.

### Остановить / перезапустить

```bash
systemctl stop funpay-ns-bot
systemctl restart funpay-ns-bot
systemctl status funpay-ns-bot
```

---

## (Опционально) Tailscale — чтобы вернуть нормальный SSH

После первичной установки можно поднять Tailscale — это бесплатная P2P-VPN,
которая обходит любые блокировки провайдеров (включая твою). После установки
ты сможешь подключаться к серверу обычным `ssh` с локального ПК.

**На сервере (веб-консоль):**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh
```

Скрипт даст ссылку — открой её в браузере на ПК, залогинься через
Google/Microsoft/GitHub. Сервер привяжется к твоему Tailscale-аккаунту.

**На локальном ПК:**

Качай и ставь Tailscale: https://tailscale.com/download/windows
Логинься тем же аккаунтом.

После этого:

```powershell
ssh root@funpay-ns-bot    # имя берётся из Tailscale, или его внутренний IP
```

И всё работает напрямую — SSH, scp, что угодно.

---

## Безопасность

После первого входа в веб-консоли смени root-пароль:

```bash
passwd
```

И, когда настроишь Tailscale, отключи пароли в SSH:

```bash
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

Тогда вход только по ключам через Tailscale — максимально безопасно.
