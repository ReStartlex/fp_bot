#!/usr/bin/env bash
# Скачивание/обновление кода проекта в /opt/funpay-ns-bot.
# Использовать, когда git clone github.com напрямую не работает (как в нашем
# случае: Timeweb режет TCP к github.com).
#
# Стратегия (по приоритету):
#   1. CODEBERG_URL (если задан) — git clone/pull от него.
#   2. Git clone/fetch через gh-proxy.com (smart HTTP протокол не кэшируется).
#   3. Tarball через прокси с двойным cache-bust (fallback last resort).

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot
BRANCH=${BRANCH:-main}
GH_OWNER=ReStartlex
GH_REPO=fp_bot

CODEBERG_URL=${CODEBERG_URL:-}
GH_PROXY=${GH_PROXY:-https://gh-proxy.com}
TARBALL_URL="${GH_PROXY}/https://github.com/${GH_OWNER}/${GH_REPO}/archive/refs/heads/${BRANCH}.tar.gz"
GIT_PROXY_URL="${GH_PROXY}/https://github.com/${GH_OWNER}/${GH_REPO}.git"

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

# ─── Стратегия 2: git через прокси (smart HTTP не кэшируется) ───
# gh-proxy.com поддерживает git smart-http. git fetch/clone тянет
# объекты пакетами и НИКОГДА не отдаёт устаревший snapshot.
echo "    Пробую git fetch через прокси: ${GIT_PROXY_URL}"
if [[ -d "${APP_DIR}/.git" ]]; then
    # Уже клонировано — просто fetch+reset
    cd "${APP_DIR}"
    if git config --get remote.origin.url | grep -q "${GH_PROXY}"; then
        : # remote уже через прокси — ничего не меняем
    else
        git remote set-url origin "${GIT_PROXY_URL}"
    fi
    if git fetch --depth=1 origin "${BRANCH}" && \
       git reset --hard "origin/${BRANCH}"; then
        echo "    [git] fetch+reset через прокси: OK"
        # Восстановим права после git операций
        if getent passwd bot >/dev/null 2>&1; then
            chown -R bot:bot "${APP_DIR}"
            chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
        fi
        # BUILD_INFO из src/_version.py
        if [[ -f "${APP_DIR}/src/_version.py" ]]; then
            SHA=$(grep -E '^SHA' "${APP_DIR}/src/_version.py" | head -1 \
                  | sed -E 's/.*"([^"]+)".*/\1/')
            DATE=$(grep -E '^DATE' "${APP_DIR}/src/_version.py" | head -1 \
                  | sed -E 's/.*"([^"]+)".*/\1/')
            SUBJECT=$(grep -E '^SUBJECT' "${APP_DIR}/src/_version.py" | head -1 \
                  | sed -E 's/.*"(.*)".*/\1/')
            {
                echo "sha=${SHA}"
                echo "branch=${BRANCH}"
                echo "date=${DATE}"
                echo "subject=${SUBJECT}"
                echo "fetched_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            } > "${APP_DIR}/BUILD_INFO"
            echo "    BUILD_INFO: ${SHA:0:12}  ${DATE:-?}  ${SUBJECT:-?}"
        fi
        echo "    Готово через git+proxy."
        exit 0
    fi
    echo "    [git] fetch+reset не удался — fallback на свежий clone"
    cd /tmp
fi

# Свежий clone в /tmp, потом aтомарно подменяем APP_DIR (сохраняя .env и data)
echo "    [git] делаю свежий clone через прокси…"
TMP_CLONE="/tmp/fp_bot_clone_$(date +%s)"
rm -rf "${TMP_CLONE}"
if git clone --depth=1 --branch "${BRANCH}" \
    "${GIT_PROXY_URL}" "${TMP_CLONE}" 2>&1; then
    # Сохраняем .env и data
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
    # Сохраняем .venv (не клонировать его заново это медленно)
    PRESERVE_VENV=""
    if [[ -d "${APP_DIR}/.venv" ]]; then
        PRESERVE_VENV="${APP_DIR}.venv_$$"
        mv "${APP_DIR}/.venv" "${PRESERVE_VENV}"
    fi
    rm -rf "${APP_DIR}"
    mv "${TMP_CLONE}" "${APP_DIR}"
    # Возвращаем .env, data, .venv
    if [[ -n "${PRESERVE_ENV}" ]]; then
        mv "${PRESERVE_ENV}" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env"
    fi
    if [[ -n "${PRESERVE_DATA}" ]]; then
        mkdir -p "${APP_DIR}/data"
        cp -a "${PRESERVE_DATA}/." "${APP_DIR}/data/" 2>/dev/null || true
        rm -rf "${PRESERVE_DATA}"
    fi
    if [[ -n "${PRESERVE_VENV}" ]]; then
        mv "${PRESERVE_VENV}" "${APP_DIR}/.venv"
    fi
    if getent passwd bot >/dev/null 2>&1; then
        chown -R bot:bot "${APP_DIR}"
        chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
    fi
    if [[ -f "${APP_DIR}/src/_version.py" ]]; then
        SHA=$(grep -E '^SHA' "${APP_DIR}/src/_version.py" | head -1 \
              | sed -E 's/.*"([^"]+)".*/\1/')
        DATE=$(grep -E '^DATE' "${APP_DIR}/src/_version.py" | head -1 \
              | sed -E 's/.*"([^"]+)".*/\1/')
        SUBJECT=$(grep -E '^SUBJECT' "${APP_DIR}/src/_version.py" | head -1 \
              | sed -E 's/.*"(.*)".*/\1/')
        {
            echo "sha=${SHA}"
            echo "branch=${BRANCH}"
            echo "date=${DATE}"
            echo "subject=${SUBJECT}"
            echo "fetched_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        } > "${APP_DIR}/BUILD_INFO"
        echo "    BUILD_INFO: ${SHA:0:12}  ${DATE:-?}  ${SUBJECT:-?}"
    fi
    echo "    Готово через git clone+proxy."
    exit 0
else
    echo "    [git] clone через прокси упал — fallback на tarball"
    rm -rf "${TMP_CLONE}"
fi

# gh-proxy.com и подобные прокси нередко КЭШИРУЮТ URL'ы вида
# /archive/refs/heads/main.tar.gz — на VPS прилетал устаревший tarball
# даже через 30+ минут после push'а в GitHub.
#
# Чтобы гарантированно получить свежий код, действуем в три эшелона:
#
#   1. Пробуем узнать актуальный SHA main через api.github.com:
#      a) НАПРЯМУЮ (часто доступно даже если github.com заблокирован)
#      b) через gh-proxy.com
#      c) через raw.githubusercontent.com нашего же src/_version.py
#         (он обновляется при каждом push'е через `stamp_version.py`)
#   2. Если SHA удалось добыть — тянем tarball по
#      /archive/${SHA}.tar.gz: такой URL уникален для каждого коммита
#      и физически не может быть закэширован.
#   3. Если SHA не получили — добавляем timestamp как query-string
#      к branch-URL. gh-proxy.com включает query-string в кэш-ключ,
#      так что это тоже даёт cache-miss.
LATEST_SHA=""
echo "    Пробую узнать актуальный SHA main…"

# (a) api.github.com напрямую
if [[ -z "${LATEST_SHA}" ]]; then
    if SHA_JSON=$(curl -fsSL --max-time 15 -H 'Cache-Control: no-cache' \
        "https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/commits/${BRANCH}" \
        2>/dev/null); then
        LATEST_SHA=$(echo "${SHA_JSON}" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    sys.stdout.write(data.get("sha", "") or "")
except Exception:
    pass
' 2>/dev/null || true)
        if [[ -n "${LATEST_SHA}" ]]; then
            echo "    [a] SHA через api.github.com напрямую: ${LATEST_SHA:0:12}"
        fi
    fi
fi

# (b) api.github.com через gh-proxy
if [[ -z "${LATEST_SHA}" ]]; then
    if SHA_JSON=$(curl -fsSL --max-time 15 -H 'Cache-Control: no-cache' \
        "${GH_PROXY}/https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/commits/${BRANCH}?nc=$(date +%s)" \
        2>/dev/null); then
        LATEST_SHA=$(echo "${SHA_JSON}" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    sys.stdout.write(data.get("sha", "") or "")
except Exception:
    pass
' 2>/dev/null || true)
        if [[ -n "${LATEST_SHA}" ]]; then
            echo "    [b] SHA через gh-proxy → api.github.com: ${LATEST_SHA:0:12}"
        fi
    fi
fi

# (c) read SHA из src/_version.py через raw.githubusercontent с cache-bust
if [[ -z "${LATEST_SHA}" ]]; then
    VERSION_URL="${GH_PROXY}/https://raw.githubusercontent.com/${GH_OWNER}/${GH_REPO}/${BRANCH}/src/_version.py?nc=$(date +%s)"
    if VERSION_PY=$(curl -fsSL --max-time 15 -H 'Cache-Control: no-cache' \
        "${VERSION_URL}" 2>/dev/null); then
        LATEST_SHA=$(echo "${VERSION_PY}" | grep -E '^SHA' | head -1 \
            | sed -E 's/.*"([^"]+)".*/\1/' || true)
        if [[ -n "${LATEST_SHA}" ]]; then
            echo "    [c] SHA через raw _version.py: ${LATEST_SHA:0:12}"
        fi
    fi
fi

# Выбираем URL tarball
if [[ -n "${LATEST_SHA}" ]]; then
    TARBALL_URL="${GH_PROXY}/https://github.com/${GH_OWNER}/${GH_REPO}/archive/${LATEST_SHA}.tar.gz"
    echo "    → tarball по SHA: ${TARBALL_URL}"
else
    # Fallback: query-string как cache-bust
    TARBALL_URL="${GH_PROXY}/https://github.com/${GH_OWNER}/${GH_REPO}/archive/refs/heads/${BRANCH}.tar.gz?nc=$(date +%s)"
    echo "    WARN: SHA не получили из API/raw — использую branch+query cache-bust"
    echo "    → ${TARBALL_URL}"
fi

TMP_TARBALL="/tmp/fp_bot_${BRANCH}_$(date +%s).tar.gz"
rm -f "${TMP_TARBALL}"
curl -fL --max-time 60 -H 'Cache-Control: no-cache' \
    -o "${TMP_TARBALL}" "${TARBALL_URL}"

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

# Записываем BUILD_INFO. Источник истины — файл src/_version.py,
# который пишется при каждом push'е (см. pre-push hook / git commit).
# Если файла нет — пробуем api.github.com через прокси (он часто 403,
# поэтому это только fallback).
SHA=""
DATE=""
SUBJECT=""
if [[ -f "${APP_DIR}/src/_version.py" ]]; then
    SHA=$(grep -E '^SHA' "${APP_DIR}/src/_version.py" | head -1 \
          | sed -E 's/.*"([^"]+)".*/\1/')
    DATE=$(grep -E '^DATE' "${APP_DIR}/src/_version.py" | head -1 \
          | sed -E 's/.*"([^"]+)".*/\1/')
    SUBJECT=$(grep -E '^SUBJECT' "${APP_DIR}/src/_version.py" | head -1 \
          | sed -E 's/.*"(.*)".*/\1/')
fi
if [[ -z "${SHA}" ]]; then
    COMMITS_URL="${GH_PROXY}/https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/commits/${BRANCH}?nocache=$(date +%s)"
    if BUILD_JSON=$(curl -fsSL --max-time 30 "${COMMITS_URL}" 2>/dev/null); then
        PARSED=$(BUILD_JSON="${BUILD_JSON}" python3 -c '
import json, os
data = json.loads(os.environ["BUILD_JSON"])
sha = data.get("sha", "")
date = (data.get("commit") or {}).get("author", {}).get("date", "")
msg = ((data.get("commit") or {}).get("message") or "").splitlines()[0][:160]
print(f"{sha}|{date}|{msg}")
' 2>/dev/null) || PARSED=""
        IFS='|' read -r SHA DATE SUBJECT <<< "${PARSED}"
    fi
fi
if [[ -n "${SHA}" ]]; then
    {
        echo "sha=${SHA}"
        echo "branch=${BRANCH}"
        echo "date=${DATE}"
        echo "subject=${SUBJECT}"
        echo "fetched_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "${APP_DIR}/BUILD_INFO"
    echo "    BUILD_INFO: ${SHA:0:12}  ${DATE:-?}  ${SUBJECT:-?}"

    # САНИТИ: если ожидали один SHA, но получили другой — кричим.
    if [[ -n "${LATEST_SHA:-}" && "${SHA}" != "${LATEST_SHA}" ]]; then
        echo
        echo "    !!! ВНИМАНИЕ: ожидали SHA ${LATEST_SHA:0:12}, а в tarball'е ${SHA:0:12}"
        echo "    Прокси скорее всего отдал устаревший архив. Попробуй ещё раз"
        echo "    через минуту или используй CODEBERG_URL=… ./fetch_code.sh"
    fi
else
    echo "    WARN: BUILD_INFO не записан (нет src/_version.py и api.github.com недоступен)"
fi

echo "    Готово через gh-proxy."
