#!/usr/bin/env bash
# Скачивание/обновление кода проекта в /opt/funpay-ns-bot.
# Использовать, когда git clone github.com напрямую не работает (как в нашем
# случае: Timeweb режет TCP к github.com).
#
# Стратегия:
#   1. Пробуем git clone/pull с CODEBERG_URL (если он задан).
#   2. Если падает или не задан — качаем tarball через gh-proxy.com.

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot
BRANCH=${BRANCH:-main}
GH_OWNER=ReStartlex
GH_REPO=fp_bot

CODEBERG_URL=${CODEBERG_URL:-}
GH_PROXY=${GH_PROXY:-https://gh-proxy.com}
TARBALL_URL="${GH_PROXY}/https://github.com/${GH_OWNER}/${GH_REPO}/archive/refs/heads/${BRANCH}.tar.gz"

echo "==> Получаем код в ${APP_DIR}"

if [[ -n "${CODEBERG_URL}" ]]; then
    echo "    Использую Codeberg-зеркало: ${CODEBERG_URL}"
    if [[ -d "${APP_DIR}/.git" ]]; then
        cd "${APP_DIR}"
        git fetch origin "${BRANCH}"
        git reset --hard "origin/${BRANCH}"
    else
        rm -rf "${APP_DIR}"
        git clone --branch "${BRANCH}" "${CODEBERG_URL}" "${APP_DIR}"
    fi
    echo "    Готово через Codeberg."
    exit 0
fi

echo "    Качаю tarball: ${TARBALL_URL}"
TMP_TARBALL="/tmp/fp_bot_${BRANCH}.tar.gz"
rm -f "${TMP_TARBALL}"
curl -fL --max-time 60 -o "${TMP_TARBALL}" "${TARBALL_URL}"

# Сохраняем .env и data/logs если уже есть
PRESERVE_ENV=""
if [[ -f "${APP_DIR}/.env" ]]; then
    PRESERVE_ENV=$(mktemp)
    cp "${APP_DIR}/.env" "${PRESERVE_ENV}"
fi
PRESERVE_DATA=""
if [[ -d "${APP_DIR}/data" ]]; then
    PRESERVE_DATA=$(mktemp -d)
    cp -a "${APP_DIR}/data/." "${PRESERVE_DATA}/" 2>/dev/null || true
fi

mkdir -p "${APP_DIR}"
# Распаковываем поверх с --strip-components чтобы убрать верхний каталог fp_bot-main/
tar -xzf "${TMP_TARBALL}" -C "${APP_DIR}" --strip-components=1
rm -f "${TMP_TARBALL}"

# Восстанавливаем .env и data
if [[ -n "${PRESERVE_ENV}" ]]; then
    mv "${PRESERVE_ENV}" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
fi
if [[ -n "${PRESERVE_DATA}" ]]; then
    mkdir -p "${APP_DIR}/data"
    cp -a "${PRESERVE_DATA}/." "${APP_DIR}/data/" 2>/dev/null || true
    rm -rf "${PRESERVE_DATA}"
fi

# ВАЖНО: tar/mv из /tmp создают файлы от текущего юзера (обычно root).
# Без этого systemd-сервис (бежит от 'bot') получает Permission denied
# на чтение .env и падает с PermissionError ещё до собственных логов.
# Поэтому всегда возвращаем владельца bot:bot, если такой юзер есть.
if getent passwd bot >/dev/null 2>&1; then
    chown -R bot:bot "${APP_DIR}"
    chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
    echo "    chown bot:bot ${APP_DIR} — ОК"
fi

# Записываем BUILD_INFO для /version и update.sh. Тянем последний коммит
# main через gh-proxy (тарбол сам по себе SHA не содержит). Парсим JSON
# через python3 (он всегда есть на VPS), потому что grep на бинарных
# полях msg/title капризничает с многострочными значениями и юникодом.
COMMITS_URL="${GH_PROXY}/https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/commits/${BRANCH}?nocache=$(date +%s)"
if BUILD_JSON=$(curl -fsSL --max-time 30 "${COMMITS_URL}" 2>&1); then
    # ВАЖНО: НЕ используем heredoc одновременно с pipe — bash отдаст
    # python3 в stdin одно из двух, и парсер развалится. Передаём JSON
    # через переменную окружения, а скрипт даём флагом -c.
    PARSED=$(BUILD_JSON="${BUILD_JSON}" python3 -c '
import json, os
data = json.loads(os.environ["BUILD_JSON"])
sha = data.get("sha", "")
date = (data.get("commit") or {}).get("author", {}).get("date", "")
msg = ((data.get("commit") or {}).get("message") or "").splitlines()[0][:160]
print(f"{sha}|{date}|{msg}")
' 2>&1) || PARSED=""
    IFS='|' read -r SHA DATE SUBJECT <<< "${PARSED}"
    if [[ -n "${SHA}" && "${SHA}" != *"error"* && "${SHA}" != *"Traceback"* ]]; then
        {
            echo "sha=${SHA}"
            echo "branch=${BRANCH}"
            echo "date=${DATE}"
            echo "subject=${SUBJECT}"
            echo "fetched_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        } > "${APP_DIR}/BUILD_INFO"
        echo "    BUILD_INFO: ${SHA:0:12}  ${DATE}  ${SUBJECT}"
    else
        echo "    WARN: BUILD_INFO не записан. Ответ python3: ${PARSED:0:200}"
        echo "    WARN: первые 200 байт ответа GitHub: ${BUILD_JSON:0:200}"
    fi
else
    echo "    WARN: BUILD_INFO не записан (curl к api.github.com упал): ${BUILD_JSON:0:200}"
fi

echo "    Готово через gh-proxy."
