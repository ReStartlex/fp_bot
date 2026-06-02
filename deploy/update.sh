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
#
# Полезные env-переменные:
#   PIN_SHA=<sha>           Зафиксировать обновление на конкретный коммит
#                           (защита от случайного отката на «плохой» main).
#   GIT_HTTP_PROXY=<url>    HTTP-прокси для git напрямую на github.com
#                           (fallback, если gh-proxy.com на тех. работах).
#   BACKUP_KEEP=<N>         Сколько последних бэкапов держать (default 10).
#   STAGING_DIR=<path>      Где собирать staging (default ${APP_DIR}.staging).
#
# Архитектура (важно):
#   1. БЭКАП (.env + bridge.db) — пока сервис ещё ЖИВ и БД консистентна.
#   2. FETCH в STAGING (а не in-place). Сервис продолжает работать.
#   3. VERIFY staging (compileall + ключевые файлы есть). Если verify
#      падает — exit БЕЗ остановки сервиса. Бот продолжает работать на
#      старом коде. Это спасает от инцидента 23.05.2026: тогда update.sh
#      сначала остановил сервис, потом git fetch завис на мёртвом прокси,
#      и продакшн остался лежать.
#   4. STOP сервиса (только теперь, когда новый код гарантированно валиден).
#   5. RSYNC staging → production (с теми же exclude, что у tarball-деплоя).
#   6. PIP install + chown + START сервиса.
#   7. Health-check.

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot
STAGING_DIR=${STAGING_DIR:-${APP_DIR}.staging}
BACKUP_KEEP=${BACKUP_KEEP:-10}

if [[ ! -d "${APP_DIR}" ]]; then
    echo "ОШИБКА: ${APP_DIR} не существует. Запусти deploy/fetch_code.sh + bootstrap.sh."
    exit 1
fi

# git 2.35+ ругается на "dubious ownership" если папка принадлежит
# другому пользователю (bot:bot, а update.sh — от root). Регистрируем
# safe.directory один раз в системном config.
git config --system --add safe.directory "${APP_DIR}" 2>/dev/null \
    || git config --global --add safe.directory "${APP_DIR}" 2>/dev/null \
    || true
git config --system --add safe.directory "${STAGING_DIR}" 2>/dev/null \
    || git config --global --add safe.directory "${STAGING_DIR}" 2>/dev/null \
    || true

# update.sh обязан запускаться от root. Сам скрипт делает chown bot:bot,
# systemctl restart funpay-ns-bot и chmod 600 .env — всё это требует root.
# Если запустили из-под bot — пере-вызовем себя через sudo.
if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        echo "==> update.sh требует root, перезапускаю через sudo"
        exec sudo -E bash "$0" "$@"
    else
        echo "ОШИБКА: update.sh должен запускаться от root (нужны chown, systemctl, chmod на .env)."
        echo "Запусти: sudo bash ${APP_DIR}/deploy/update.sh"
        exit 1
    fi
fi

# 1. БЭКАП ДО ВСЕГО. Делаем пока сервис ещё жив и БД (включая WAL/SHM)
# в консистентном состоянии — копия нам пригодится для отката, если
# на новом коде сервис не поднимется.
BACKUP_DIR_LINE=""
if [[ -x "${APP_DIR}/.venv/bin/python" && -f "${APP_DIR}/deploy/runtime.py" ]]; then
    echo "==> Делаю бэкап .env + bridge.db (keep=${BACKUP_KEEP})"
    if BACKUP_DIR_LINE=$(
        cd "${APP_DIR}" && "${APP_DIR}/.venv/bin/python" -m deploy.runtime backup \
            --app-dir "${APP_DIR}" --keep "${BACKUP_KEEP}" \
            | tee /dev/stderr | grep '^BACKUP_DIR=' || true
    ); then
        BACKUP_DIR=$(printf '%s' "${BACKUP_DIR_LINE}" | sed -E 's/^BACKUP_DIR=//')
        echo "    OK: ${BACKUP_DIR}"
    else
        echo "    ВНИМАНИЕ: бэкап упал, продолжаю без него."
        BACKUP_DIR=""
    fi
else
    echo "==> deploy.runtime недоступен (свежий bootstrap?) — пропускаю бэкап"
    BACKUP_DIR=""
fi

# 2. FETCH в STAGING. Бот продолжает работать.
#    PIN_SHA / GIT_HTTP_PROXY пробрасываются прозрачно через env.
#    fetch_code.sh теперь смотрит APP_DIR — указываем staging.
#    PROD_APP_DIR — production-каталог, откуда fetch_code.sh читает
#    .deploy_pin (в staging его нет, он лежит только в production).
echo "==> Fetch кода в staging: ${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"
if APP_DIR="${STAGING_DIR}" PROD_APP_DIR="${APP_DIR}" bash "${APP_DIR}/deploy/fetch_code.sh"; then
    echo "    staging fetch OK"
else
    echo
    echo "── ❌ FETCH В STAGING УПАЛ ──"
    echo "Сервис funpay-ns-bot НЕ был остановлен и продолжает работать"
    echo "на старом коде. Это безопасный сценарий: ничего трогать не нужно."
    echo
    echo "Проверь сеть/прокси и запусти update.sh повторно, либо выкати"
    echo "tarball-стратегией вручную (см. PROJECT_CONTEXT.md)."
    echo "─────────────────────────────"
    exit 1
fi

# 3. VERIFY staging. Только если zero errors — продолжаем в production.
#    На любой ошибке staging — exit БЕЗ остановки сервиса.
echo "==> Verify staging"
VERIFY_PYTHON=""
if [[ -x "${APP_DIR}/.venv/bin/python" ]]; then
    VERIFY_PYTHON="${APP_DIR}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    VERIFY_PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    VERIFY_PYTHON="python"
fi
if [[ -n "${VERIFY_PYTHON}" && -f "${STAGING_DIR}/deploy/runtime.py" ]]; then
    set +e
    (cd "${STAGING_DIR}" && "${VERIFY_PYTHON}" -m deploy.runtime verify-staging --dir "${STAGING_DIR}")
    VERIFY_RC=$?
    set -e
    if [[ "${VERIFY_RC}" -ne 0 ]]; then
        echo
        echo "── ❌ VERIFY STAGING УПАЛ (rc=${VERIFY_RC}) ──"
        echo "Сервис funpay-ns-bot НЕ был остановлен и продолжает работать"
        echo "на старом коде. Staging-папка осталась в ${STAGING_DIR}"
        echo "для ручного разбора."
        echo "──────────────────────────────────────────────"
        exit 1
    fi
    echo "    verify OK"
else
    echo "    (verify пропущен: deploy.runtime недоступен)"
fi

# 4. STOP сервиса. Только теперь — staging валиден и готов к swap'у.
# Останавливаем потому что процесс держит open file descriptor на bridge.db;
# после фоновой подмены файлов SQLite видит "файл удалён" и переключается
# в read-only режим (отсюда наш "attempt to write a readonly database").
SERVICE_WAS_RUNNING=0
API_WAS_RUNNING=0
if systemctl is-active --quiet funpay-ns-api 2>/dev/null; then
    echo "==> Останавливаю funpay-ns-api до swap'a кода"
    systemctl stop funpay-ns-api
    API_WAS_RUNNING=1
fi
if systemctl is-active --quiet funpay-ns-bot 2>/dev/null; then
    echo "==> Останавливаю funpay-ns-bot до swap'a кода"
    systemctl stop funpay-ns-bot
    SERVICE_WAS_RUNNING=1
fi

# 5. RSYNC staging → production.
# Те же exclude, что у tarball-деплоя: .env, data/, logs/, backups/,
# .venv/, .git/, .deploy_pin (его обновим отдельно ниже).
# --delete: убирает в проде файлы, которых уже нет в новом коммите.
echo "==> rsync staging → production"
rsync -a --delete \
    --exclude='.env' --exclude='data/' --exclude='logs/' --exclude='backups/' \
    --exclude='.venv/' --exclude='.git/' --exclude='.deploy_pin' \
    --exclude='BUILD_INFO' --exclude='__pycache__/' --exclude='*.pyc' \
    "${STAGING_DIR}/" "${APP_DIR}/"

# Чистим bytecode на проде, чтобы Python не подсунул кеш на старый код
find "${APP_DIR}/src" "${APP_DIR}/tests" -name '__pycache__' -type d \
    -exec rm -rf {} + 2>/dev/null || true
find "${APP_DIR}/src" "${APP_DIR}/tests" -name '*.pyc' \
    -delete 2>/dev/null || true

# Обновим .deploy_pin и BUILD_INFO на проде из staging
if [[ -f "${STAGING_DIR}/.deploy_pin" ]]; then
    cp "${STAGING_DIR}/.deploy_pin" "${APP_DIR}/.deploy_pin"
fi
if [[ -f "${STAGING_DIR}/BUILD_INFO" ]]; then
    cp "${STAGING_DIR}/BUILD_INFO" "${APP_DIR}/BUILD_INFO"
fi

# 6. Обновляем pip-зависимости. Сервис уже остановлен, .venv свободна.
# НЕ глотаем stderr: при настоящих ошибках их видно в логе.
PIP_OK=1
if ! "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"; then
    PIP_OK=0
    echo
    echo "ВНИМАНИЕ: pip install упал. Возможно, конфликт версий в"
    echo "requirements.txt. Сервис будет запущен на СТАРЫХ зависимостях"
    echo "и новом коде — если новые модули не используются, всё сработает."
    echo "Иначе посмотри вывод выше и поправь requirements.txt."
fi

# 7. Чиним права ДО рестарта. Сервис бежит от 'bot', и .env должен
# быть владельца bot (mv от root забирает права).
# Также гарантируем logs/ и data/ как папки (systemd-mount-namespacing
# падает если их нет) и владельца bot:bot.
mkdir -p "${APP_DIR}/logs" "${APP_DIR}/data" "${APP_DIR}/backups"
if getent passwd bot >/dev/null 2>&1; then
    chown -R bot:bot "${APP_DIR}"
fi
chmod 700 "${APP_DIR}/logs" "${APP_DIR}/data" "${APP_DIR}/backups" 2>/dev/null || true
chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
# Убеждаемся, что bridge.db и его WAL/SHM журналы доступны на запись
# для bot (после prev неудачных rm -rf могло слететь).
find "${APP_DIR}/data" -type f \( -name 'bridge.db' -o -name 'bridge.db-*' \) \
    -exec chmod 600 {} \; 2>/dev/null || true
# Бэкап-каталоги (.env, bridge.db) — тоже только для bot, секреты.
find "${APP_DIR}/backups" -type f -exec chmod 600 {} \; 2>/dev/null || true

# Обновляем systemd units, если они появились/изменились в свежем коде.
if [[ -f "${APP_DIR}/deploy/funpay-ns-bot.service" ]]; then
    cp "${APP_DIR}/deploy/funpay-ns-bot.service" /etc/systemd/system/funpay-ns-bot.service
fi
if [[ -f "${APP_DIR}/deploy/funpay-ns-api.service" ]]; then
    cp "${APP_DIR}/deploy/funpay-ns-api.service" /etc/systemd/system/funpay-ns-api.service
fi
systemctl daemon-reload

# Печатает короткую инструкцию по откату из последнего бэкапа.
# Вызывается, если сервис не поднялся после обновления.
_print_rollback_hint() {
    local hint_backup="${BACKUP_DIR:-}"
    echo
    echo "── 🚑 КАК ОТКАТИТЬСЯ ──"
    if [[ -n "${hint_backup}" && -d "${hint_backup}" ]]; then
        echo "  systemctl stop funpay-ns-bot funpay-ns-api"
        if [[ -f "${hint_backup}/.env" ]]; then
            echo "  cp '${hint_backup}/.env' ${APP_DIR}/.env"
        fi
        if [[ -f "${hint_backup}/data/bridge.db" ]]; then
            echo "  cp '${hint_backup}/data/bridge.db' ${APP_DIR}/data/bridge.db"
        fi
        echo "  # код:"
        echo "  PIN_SHA=<sha_прошлой_рабочей_версии> bash ${APP_DIR}/deploy/update.sh"
    else
        echo "  Бэкапа этого update'а нет, смотри ${APP_DIR}/backups/ вручную."
    fi
    echo "──────────────────────"
}

# 8. Запускаем (или перезапускаем) сервис.
if systemctl is-enabled --quiet funpay-ns-bot 2>/dev/null \
   || [[ "${SERVICE_WAS_RUNNING}" -eq 1 ]]; then
    systemctl start funpay-ns-bot
    echo "Сервис funpay-ns-bot запущен."
    sleep 4
    if ! systemctl is-active --quiet funpay-ns-bot; then
        echo
        echo "── ВНИМАНИЕ: сервис не поднялся, последние 60 строк лога ──"
        journalctl -u funpay-ns-bot -n 60 --no-pager
        echo "──────────────────────────────────────────────────────────"
        _print_rollback_hint
    fi
fi

if systemctl is-enabled --quiet funpay-ns-api 2>/dev/null \
   || [[ "${API_WAS_RUNNING}" -eq 1 ]]; then
    systemctl start funpay-ns-api
    echo "Сервис funpay-ns-api запущен."
    API_READY=0
    for _ in $(seq 1 12); do
        if "${APP_DIR}/.venv/bin/python" -m src.tools.check_web_api >/dev/null 2>&1; then
            API_READY=1
            break
        fi
        sleep 1
    done
    if ! systemctl is-active --quiet funpay-ns-api; then
        echo
        echo "── ВНИМАНИЕ: API сервис не поднялся, последние 60 строк лога ──"
        journalctl -u funpay-ns-api -n 60 --no-pager
        echo "────────────────────────────────────────────────────────────"
        _print_rollback_hint
    elif [[ "${API_READY}" -eq 1 ]]; then
        echo "API health-check: OK."
    else
        echo "ВНИМАНИЕ: API сервис активен, но /healthz не ответил за 12 секунд."
        journalctl -u funpay-ns-api -n 30 --no-pager || true
    fi
fi

# 9. Печатаем версию, чтобы сразу было видно — фикс задеплоился?
if [[ -f "${APP_DIR}/BUILD_INFO" ]]; then
    echo
    echo "── Версия задеплоенного кода ──"
    cat "${APP_DIR}/BUILD_INFO"
    echo "───────────────────────────────"
fi

if [[ -n "${BACKUP_DIR}" ]]; then
    echo "Бэкап перед апдейтом: ${BACKUP_DIR}"
fi

echo "Обновление завершено."
