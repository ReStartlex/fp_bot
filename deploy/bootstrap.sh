#!/usr/bin/env bash
# Bootstrap-скрипт.
# Предполагает, что код проекта уже распакован в /opt/funpay-ns-bot
# (см. deploy/fetch_code.sh — скачивает через gh-proxy.com).
#
# Что делает:
#   1. apt update/upgrade
#   2. ставит python3.12, pip, venv, git, unzip, ufw, fail2ban
#   3. создаёт пользователя bot
#   4. создаёт venv + ставит pip-зависимости
#   5. настраивает firewall и fail2ban
#   6. устанавливает systemd-юнит

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot

if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
    echo "ОШИБКА: ${APP_DIR}/requirements.txt не найден."
    echo "Сначала скачай код: bash ${APP_DIR}/deploy/fetch_code.sh (или вручную)."
    exit 1
fi

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

mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"

echo "==> 4/6 Виртуальное окружение и зависимости"
if [[ ! -d "${APP_DIR}/.venv" ]]; then
    python3.12 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/pip" install --upgrade pip wheel
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

chown -R bot:bot "${APP_DIR}"

echo "==> 5/6 Firewall (открыт только SSH 22 + 443) и fail2ban"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 443/tcp
ufw --force enable >/dev/null
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban

echo "==> 6/6 systemd-юнит"
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
