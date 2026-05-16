#!/usr/bin/env bash
# Первичная настройка чистого Ubuntu 24.04 VPS.
# Запускать ОДИН РАЗ от root сразу после первого SSH-входа.
#
# Что делает:
#   1. Обновляет пакеты
#   2. Ставит python3.12 + venv + pip + git + unzip
#   3. Включает ufw (firewall): открыт только SSH
#   4. Ставит fail2ban против брутфорса SSH
#   5. Настраивает таймзону Europe/Moscow
#   6. Создаёт каталог /opt/funpay-ns-bot и системного пользователя bot
#
# Запуск:
#   bash setup_server.sh

set -euo pipefail

echo "==> Обновление пакетов"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade

echo "==> Установка зависимостей"
apt-get install -y \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    python3-pip \
    git \
    unzip \
    curl \
    htop \
    ufw \
    fail2ban \
    ca-certificates \
    tzdata

echo "==> Таймзона Europe/Moscow"
timedatectl set-timezone Europe/Moscow

echo "==> Создание пользователя bot"
if ! id -u bot >/dev/null 2>&1; then
    useradd -m -s /bin/bash bot
fi

echo "==> Каталог приложения /opt/funpay-ns-bot"
mkdir -p /opt/funpay-ns-bot
chown -R bot:bot /opt/funpay-ns-bot

echo "==> Firewall (ufw): открываем только SSH"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
ufw status verbose

echo "==> fail2ban"
systemctl enable fail2ban
systemctl restart fail2ban

echo
echo "============================================================"
echo "Готово. Что дальше:"
echo "  1. На локальном ПК запусти deploy/pack.ps1 — получишь app.zip"
echo "  2. Передай app.zip и .env на сервер через scp:"
echo "       scp app.zip root@85.239.42.127:/opt/funpay-ns-bot/"
echo "       scp .env   root@85.239.42.127:/opt/funpay-ns-bot/"
echo "  3. На сервере выполни:"
echo "       cd /opt/funpay-ns-bot && bash install_app.sh"
echo "============================================================"
