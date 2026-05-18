#!/usr/bin/env bash
# Обновление кода на сервере (когда я запушил новую версию в GitHub).
# Запускать от root в веб-консоли Timeweb:
#
#   bash /opt/funpay-ns-bot/deploy/update.sh
#
# Если /opt/funpay-ns-bot/deploy/update.sh ещё нет (сильно старая версия) —
# выполни одной строкой:
#
#   curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/ReStartlex/fp_bot/main/deploy/update.sh | bash

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot

if [[ ! -d "${APP_DIR}" ]]; then
    echo "ОШИБКА: ${APP_DIR} не существует. Запусти deploy/fetch_code.sh + bootstrap.sh."
    exit 1
fi

# 1. Скачиваем свежий код, сохраняя .env и data.
# Сначала пробуем локальный fetch_code.sh. Если его нет — тянем свежий
# с GitHub (через proxy, потому что Timeweb-сеть GitHub блокирует).
# stderr НЕ глотаем: при настоящих ошибках их видно в логе.
if [[ -x "${APP_DIR}/deploy/fetch_code.sh" ]]; then
    bash "${APP_DIR}/deploy/fetch_code.sh"
else
    echo "==> локального fetch_code.sh нет, тяну с GitHub через gh-proxy"
    bash <(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/ReStartlex/fp_bot/main/deploy/fetch_code.sh)
fi

# 2. Обновляем pip-зависимости (если изменились)
"${APP_DIR}/.venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

# 3. Если systemd-сервис запущен — рестартуем
if systemctl is-active --quiet funpay-ns-bot 2>/dev/null; then
    systemctl restart funpay-ns-bot
    echo "Сервис funpay-ns-bot перезапущен."
fi

# 4. Чиним права (если что-то осталось от tarball)
chown -R bot:bot "${APP_DIR}"
chmod 600 "${APP_DIR}/.env" 2>/dev/null || true

echo "Обновление завершено."
