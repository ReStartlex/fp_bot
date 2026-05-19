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

# 2. Обновляем pip-зависимости (если изменились). НЕ глотаем stderr,
# чтобы конфликты резолвера были видны сразу.
if ! "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"; then
    echo
    echo "ОШИБКА: pip install упал. Сервис НЕ перезапускаю (он сейчас работает"
    echo "на старом коде; новый код требует обновления зависимостей)."
    echo "Чаще всего это конфликт версий — посмотри вывод выше и поправь"
    echo "requirements.txt."
    exit 1
fi

# 3. Чиним права ДО рестарта. Иначе сервис бежит от 'bot', получает
# Permission denied на .env (положенный нами через mv от root) и падает
# в auto-restart loop, не оставляя собственных логов.
if getent passwd bot >/dev/null 2>&1; then
    chown -R bot:bot "${APP_DIR}"
fi
chmod 600 "${APP_DIR}/.env" 2>/dev/null || true

# 4. Только теперь — рестарт сервиса (если он управляется systemd)
if systemctl is-active --quiet funpay-ns-bot 2>/dev/null \
   || systemctl is-enabled --quiet funpay-ns-bot 2>/dev/null; then
    systemctl restart funpay-ns-bot
    echo "Сервис funpay-ns-bot перезапущен."
    # Проверяем, поднялся ли он. Если нет — сразу показываем стектрейс,
    # а не оставляем пользователя гадать.
    sleep 4
    if ! systemctl is-active --quiet funpay-ns-bot; then
        echo
        echo "── ВНИМАНИЕ: сервис не поднялся, последние 40 строк лога ──"
        journalctl -u funpay-ns-bot -n 40 --no-pager
        echo "──────────────────────────────────────────────────────────"
    fi
fi

# 5. Печатаем версию, чтобы сразу было видно — фикс задеплоился?
if [[ -f "${APP_DIR}/BUILD_INFO" ]]; then
    echo
    echo "── Версия задеплоенного кода ──"
    cat "${APP_DIR}/BUILD_INFO"
    echo "───────────────────────────────"
fi

echo "Обновление завершено."
