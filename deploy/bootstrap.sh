#!/usr/bin/env bash
# Мастер-скрипт первичной установки бота на свежем VPS.
# Запускать ОДИН РАЗ от root в веб-консоли Timeweb:
#
#   curl -fsSL https://raw.githubusercontent.com/ReStartlex/fp_bot/main/deploy/bootstrap.sh | bash
#
# Или, если репо приватный, через git clone:
#
#   apt update && apt install -y git
#   git clone https://github.com/ReStartlex/fp_bot.git /opt/funpay-ns-bot
#   bash /opt/funpay-ns-bot/deploy/bootstrap.sh
#
# Что делает:
#   1. apt update/upgrade
#   2. ставит python3.12, pip, venv, git, unzip, ufw, fail2ban
#   3. настраивает firewall и fail2ban
#   4. клонирует репо (если ещё не клонирован)
#   5. создаёт venv + ставит зависимости
#   6. напоминает создать .env вручную

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot
REPO_URL=${REPO_URL:-https://github.com/ReStartlex/fp_bot.git}

echo "==> 1/6 Обновление пакетов"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get -y upgrade -qq

echo "==> 2/6 Установка зависимостей системы"
apt-get install -y -qq \
    python3.12 python3.12-venv python3.12-dev python3-pip \
    git unzip curl htop ufw fail2ban ca-certificates tzdata

timedatectl set-timezone Europe/Moscow

echo "==> 3/6 Пользователь bot"
if ! id -u bot >/dev/null 2>&1; then
    useradd -m -s /bin/bash bot
fi

echo "==> 4/6 Клонирование репозитория"
if [[ ! -d "${APP_DIR}/.git" ]]; then
    if [[ -d "${APP_DIR}" ]] && [[ -n "$(ls -A "${APP_DIR}" 2>/dev/null)" ]]; then
        echo "    ${APP_DIR} не пустой и не git-репо — переименовываю в ${APP_DIR}.bak"
        mv "${APP_DIR}" "${APP_DIR}.bak.$(date +%s)"
    fi
    git clone "${REPO_URL}" "${APP_DIR}"
else
    cd "${APP_DIR}"
    git pull --ff-only
fi

mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"

echo "==> 5/6 Виртуальное окружение и зависимости"
if [[ ! -d "${APP_DIR}/.venv" ]]; then
    python3.12 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/pip" install --upgrade pip wheel
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

chown -R bot:bot "${APP_DIR}"

echo "==> 6/6 Firewall (открыт только SSH) и fail2ban"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 443/tcp
ufw --force enable >/dev/null
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban

# systemd-юнит на будущее (пока не запускаем)
if [[ -f "${APP_DIR}/deploy/funpay-ns-bot.service" ]]; then
    cp "${APP_DIR}/deploy/funpay-ns-bot.service" /etc/systemd/system/funpay-ns-bot.service
    systemctl daemon-reload
fi

echo
echo "============================================================"
echo "Базовая установка завершена."
echo
echo "ОСТАЛОСЬ: создать ${APP_DIR}/.env с твоими секретами."
echo "После этого проверка NS API:"
echo "  sudo -u bot ${APP_DIR}/.venv/bin/python -m src.tools.check_ns"
echo "============================================================"
