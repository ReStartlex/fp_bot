#!/usr/bin/env bash
# Установка Cloudflare Tunnel для доступа к Web API без SSH tunnel и открытых портов.
#
# Перед запуском в Cloudflare Zero Trust создай Tunnel и Public Hostname:
#   service: http://127.0.0.1:8080
# Затем скопируй token и запусти:
#   CLOUDFLARE_TUNNEL_TOKEN='...' bash /opt/funpay-ns-bot/deploy/install_cloudflare_tunnel.sh

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot
TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-${1:-}}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ОШИБКА: скрипт нужно запускать от root."
    exit 1
fi

if [[ -z "${TOKEN}" ]]; then
    echo "ОШИБКА: не передан Cloudflare Tunnel token."
    echo "Запуск:"
    echo "  CLOUDFLARE_TUNNEL_TOKEN='...' bash ${APP_DIR}/deploy/install_cloudflare_tunnel.sh"
    exit 1
fi

echo "==> Проверяю локальный Web API"
if ! sudo -u bot "${APP_DIR}/.venv/bin/python" -m src.tools.check_web_api; then
    echo
    echo "ОШИБКА: локальный Web API не прошёл smoke-check."
    echo "Сначала проверь: systemctl status funpay-ns-api --no-pager -l"
    exit 1
fi

echo "==> Устанавливаю cloudflared"
if ! command -v cloudflared >/dev/null 2>&1; then
    install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        -o /usr/share/keyrings/cloudflare-main.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
        > /etc/apt/sources.list.d/cloudflared.list
    apt-get update
    apt-get install -y cloudflared
fi

echo "==> Регистрирую cloudflared как systemd service"
if systemctl list-unit-files cloudflared.service >/dev/null 2>&1; then
    systemctl stop cloudflared 2>/dev/null || true
fi

cloudflared service install "${TOKEN}" || {
    echo
    echo "ВНИМАНИЕ: cloudflared service install вернул ошибку."
    echo "Если сервис уже был установлен, удаляю старый и пробую ещё раз."
    cloudflared service uninstall 2>/dev/null || true
    cloudflared service install "${TOKEN}"
}

systemctl daemon-reload
systemctl enable --now cloudflared
sleep 3

echo
echo "── Статус cloudflared ──"
systemctl status cloudflared --no-pager -l || true
echo "────────────────────────"
echo
echo "Готово. Открой домен, который ты указал в Cloudflare Public Hostname."
