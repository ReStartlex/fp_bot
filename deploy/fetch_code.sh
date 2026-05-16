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

echo "    Готово через gh-proxy."
