#!/usr/bin/env bash
# Скачивание/обновление кода проекта в /opt/funpay-ns-bot.
#
# Стратегия:
#   1. CODEBERG_URL (если задан) — git clone/pull с него.
#   2. In-place git fetch + reset --hard через gh-proxy.com.
#      Делаем git операции ВНУТРИ APP_DIR, без rm -rf.
#      .venv, data/, .env, logs/ — non-tracked, остаются на месте.
#
# Не используем tarball: gh-proxy.com игнорирует SHA в URL и отдаёт
# кэшированный архив ветки. Подтверждено эмпирически.

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot
BRANCH=${BRANCH:-main}
GH_OWNER=ReStartlex
GH_REPO=fp_bot

CODEBERG_URL=${CODEBERG_URL:-}
GH_PROXY=${GH_PROXY:-https://gh-proxy.com}
GIT_PROXY_URL="${GH_PROXY}/https://github.com/${GH_OWNER}/${GH_REPO}.git"

echo "==> Получаем код в ${APP_DIR}"

# git 2.35+ отказывается работать в репозитории, владелец которого не
# совпадает с текущим юзером ("dubious ownership"). Update.sh идёт от
# root, а /opt/funpay-ns-bot принадлежит bot:bot — отсюда fatal.
# Регистрируем директорию как доверенную (идемпотентно).
git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
git config --system --add safe.directory "${APP_DIR}" 2>/dev/null || true

if [[ -n "${CODEBERG_URL}" ]]; then
    echo "    Использую Codeberg-зеркало: ${CODEBERG_URL}"
    if [[ -d "${APP_DIR}/.git" ]]; then
        cd "${APP_DIR}"
        git fetch origin "${BRANCH}"
        git reset --hard "origin/${BRANCH}"
    else
        # Codeberg доступен напрямую — сюда можем безопасно делать
        # clone (даже если APP_DIR пустой). Используем in-place init.
        cd "${APP_DIR}"
        git init -q -b "${BRANCH}"
        git remote add origin "${CODEBERG_URL}"
        git fetch --depth=1 origin "${BRANCH}"
        git reset --hard "origin/${BRANCH}"
    fi
    echo "    Готово через Codeberg."
    exit 0
fi

# ─── In-place git fetch через прокси (smart HTTP не кэшируется) ───
# git pull тянет объекты пакетом, целостность гарантируется хэшами.
echo "    Пробую git fetch через прокси: ${GIT_PROXY_URL}"

if [[ ! -d "${APP_DIR}" ]]; then
    mkdir -p "${APP_DIR}"
fi
cd "${APP_DIR}"

# Если .git ещё нет (старый tarball-deploy) — инициализируем поверх.
# git init НЕ удаляет существующие файлы. git fetch + reset --hard
# обновит tracked файлы, но non-tracked (.venv, data, .env, logs)
# останутся нетронутыми.
if [[ ! -d "${APP_DIR}/.git" ]]; then
    echo "    [git] инициализирую репо поверх существующих файлов"
    git init -q -b "${BRANCH}"
    git remote add origin "${GIT_PROXY_URL}"
fi

# Убеждаемся, что remote.origin.url через прокси (на случай миграции).
CURRENT_REMOTE=$(git config --get remote.origin.url 2>/dev/null || echo "")
if [[ "${CURRENT_REMOTE}" != "${GIT_PROXY_URL}" ]]; then
    git remote set-url origin "${GIT_PROXY_URL}" 2>/dev/null || \
        git remote add origin "${GIT_PROXY_URL}"
fi

# git fetch + reset --hard. Возможна проблема с unreachable refs
# при --depth=1 для существующего репо: shallow update сначала.
if ! git fetch --depth=1 origin "${BRANCH}"; then
    echo "    [git] fetch --depth=1 упал, пробую без --depth"
    if ! git fetch origin "${BRANCH}"; then
        echo "    [git] fetch упал — нет доступа к прокси?"
        exit 1
    fi
fi

# reset --hard перезаписывает tracked файлы. non-tracked не трогает.
if ! git reset --hard "origin/${BRANCH}"; then
    echo "    [git] reset --hard упал"
    exit 1
fi

# git clean удалит tracked-файлы, которых больше нет в репо, но
# НЕ трогает .venv, data, .env, logs (они non-tracked).
git clean -fd --exclude='.venv' --exclude='data' --exclude='.env' \
    --exclude='logs' --exclude='BUILD_INFO' 2>/dev/null || true

# Обязательные runtime-папки. systemd-сервис стартует с
# ProtectHome/InaccessiblePaths, который требует, чтобы logs/ и
# data/ существовали ДО старта. Без них unit падает с
# "Failed to set up mount namespacing: ... No such file or directory".
mkdir -p "${APP_DIR}/logs" "${APP_DIR}/data"

echo "    [git] fetch+reset через прокси: OK"

# BUILD_INFO пишем из src/_version.py — он обновляется при каждом
# нашем push'е через deploy/stamp_version.py.
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

# chown: tracked файлы от git могут быть от root, чиним обратно.
# data/ и .env: cp -a не делали, владелец и так bot (in-place fetch).
if getent passwd bot >/dev/null 2>&1; then
    chown -R bot:bot "${APP_DIR}" 2>/dev/null || true
    chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
fi

echo "    Готово через git+proxy (in-place)."
exit 0
