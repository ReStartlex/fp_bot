#!/usr/bin/env bash
# Установка/обновление приложения на сервере.
# Запускать от root в каталоге /opt/funpay-ns-bot.

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot
ZIP="${APP_DIR}/app.zip"
ENV_FILE="${APP_DIR}/.env"

cd "${APP_DIR}"

# Если есть zip — распакуем (для первой установки или обновления)
if [[ -f "${ZIP}" ]]; then
    echo "==> Распаковка app.zip"
    unzip -o "${ZIP}" -d "${APP_DIR}" >/dev/null
    rm "${ZIP}"
fi

if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
    echo "Не найден requirements.txt. Сначала загрузи app.zip через scp."
    exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Не найден ${ENV_FILE}. Сначала загрузи .env через scp."
    exit 1
fi

echo "==> Виртуальное окружение"
if [[ ! -d "${APP_DIR}/.venv" ]]; then
    python3.12 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/pip" install --upgrade pip wheel
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> Каталоги для данных и логов"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"

echo "==> Права"
chown -R bot:bot "${APP_DIR}"
chmod 600 "${ENV_FILE}"

echo "==> Установка systemd-юнита"
if [[ -f "${APP_DIR}/deploy/funpay-ns-bot.service" ]]; then
    cp "${APP_DIR}/deploy/funpay-ns-bot.service" /etc/systemd/system/funpay-ns-bot.service
    systemctl daemon-reload
    echo "    Юнит установлен. Для автозапуска: systemctl enable --now funpay-ns-bot"
fi

echo
echo "============================================================"
echo "Готово. Проверь NS API:"
echo "  sudo -u bot ${APP_DIR}/.venv/bin/python -m src.tools.check_ns"
echo "============================================================"
