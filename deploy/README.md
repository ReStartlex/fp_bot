# Deploy на VPS (через Git)

Так как у нас провайдер режет TCP к Timeweb (ping проходит, TCP — нет),
работаем через **Git + веб-консоль Timeweb**.

Сервер: Timeweb VPS `85.239.42.127`
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

## Обновление кода (когда я что-то меняю)

**Локально:**

```powershell
git add .
git commit -m "F1: FunPay client"
git push
```

**На сервере (веб-консоль):**

```bash
cd /opt/funpay-ns-bot
sudo -u bot git pull
sudo -u bot /opt/funpay-ns-bot/.venv/bin/pip install -r requirements.txt
systemctl restart funpay-ns-bot 2>/dev/null || true
```

Можно завернуть в одну строку — позже сделаем алиас.

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
