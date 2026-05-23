#!/usr/bin/env bash
# Скачивание/обновление кода проекта в /opt/funpay-ns-bot.
#
# Стратегия выбора источника (по приоритету):
#   1. CODEBERG_URL (если задан) — git clone/pull с него.
#   2. GIT_HTTP_PROXY (если задан) — git fetch напрямую с github.com
#      через HTTP-прокси. Это спасает, когда gh-proxy.com уходит на
#      тех. работы (живой инцидент: 2026-05-23).
#   3. In-place git fetch + reset --hard через gh-proxy.com (default).
#
# Стратегия выбора TARGET ref для reset --hard:
#   - переменная окружения PIN_SHA (короткий или полный SHA),
#   - либо файл <APP_DIR>/.deploy_pin (первая значимая строка),
#   - либо origin/<BRANCH> (обычно origin/main).
#
# PIN — защита от случайного повторного выкатывания «плохих» коммитов
# (см. PROJECT_CONTEXT.md → раздел про откат от 23 мая 2026).

set -euo pipefail

APP_DIR=/opt/funpay-ns-bot
BRANCH=${BRANCH:-main}
GH_OWNER=ReStartlex
GH_REPO=fp_bot

CODEBERG_URL=${CODEBERG_URL:-}
GH_PROXY=${GH_PROXY:-https://gh-proxy.com}
GIT_HTTP_PROXY=${GIT_HTTP_PROXY:-}
PIN_SHA=${PIN_SHA:-}

GIT_PROXY_URL="${GH_PROXY}/https://github.com/${GH_OWNER}/${GH_REPO}.git"
GIT_DIRECT_URL="https://github.com/${GH_OWNER}/${GH_REPO}.git"

echo "==> Получаем код в ${APP_DIR}"

# git 2.35+ отказывается работать в репозитории, владелец которого не
# совпадает с текущим юзером ("dubious ownership"). Update.sh идёт от
# root, а /opt/funpay-ns-bot принадлежит bot:bot — отсюда fatal.
# Регистрируем директорию как доверенную (идемпотентно).
git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
git config --system --add safe.directory "${APP_DIR}" 2>/dev/null || true

# Если включён прямой HTTP-прокси для git — настроим. После работы
# скрипт обязан снять proxy (см. _cleanup_proxy ниже), чтобы не
# протекать в обычный apt/curl.
_cleanup_proxy() {
    if [[ -n "${GIT_HTTP_PROXY}" ]]; then
        git config --global --unset http.proxy  2>/dev/null || true
        git config --global --unset https.proxy 2>/dev/null || true
    fi
}
trap _cleanup_proxy EXIT

if [[ -n "${GIT_HTTP_PROXY}" ]]; then
    echo "    GIT_HTTP_PROXY задан — иду напрямую на github.com через прокси"
    git config --global http.proxy  "${GIT_HTTP_PROXY}"
    git config --global https.proxy "${GIT_HTTP_PROXY}"
    REMOTE_URL="${GIT_DIRECT_URL}"
elif [[ -n "${CODEBERG_URL}" ]]; then
    REMOTE_URL="${CODEBERG_URL}"
    echo "    Использую Codeberg-зеркало: ${REMOTE_URL}"
else
    REMOTE_URL="${GIT_PROXY_URL}"
    echo "    Использую gh-proxy: ${REMOTE_URL}"
fi

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
    git remote add origin "${REMOTE_URL}"
fi

# Убеждаемся, что remote.origin.url соответствует выбранному источнику.
CURRENT_REMOTE=$(git config --get remote.origin.url 2>/dev/null || echo "")
if [[ "${CURRENT_REMOTE}" != "${REMOTE_URL}" ]]; then
    git remote set-url origin "${REMOTE_URL}" 2>/dev/null || \
        git remote add origin "${REMOTE_URL}"
fi

# Определяем целевой ref до fetch'a — чтобы понять, нужен ли --depth=1
# (для pinned SHA --depth=1 может не дотянуться до старого объекта).
RESOLVE_PIN_VENV="${APP_DIR}/.venv/bin/python"
RESOLVE_PIN_PYTHON=""
if [[ -x "${RESOLVE_PIN_VENV}" ]]; then
    RESOLVE_PIN_PYTHON="${RESOLVE_PIN_VENV}"
elif command -v python3 >/dev/null 2>&1; then
    RESOLVE_PIN_PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    RESOLVE_PIN_PYTHON="python"
fi

TARGET_REF=""
if [[ -n "${RESOLVE_PIN_PYTHON}" && -f "${APP_DIR}/deploy/runtime.py" ]]; then
    if TARGET_REF=$(
        cd "${APP_DIR}" && PIN_SHA="${PIN_SHA}" \
        "${RESOLVE_PIN_PYTHON}" -m deploy.runtime resolve-target \
            --app-dir "${APP_DIR}" --default-branch "${BRANCH}" 2>/dev/null
    ); then
        TARGET_REF="$(printf '%s' "${TARGET_REF}" | tr -d '[:space:]')"
    else
        TARGET_REF=""
    fi
fi

# Fallback резолва без Python (на самом первом bootstrap'е, когда
# .venv ещё нет). Поддерживает только PIN_SHA из env / .deploy_pin
# первая строка, без валидации SHA.
if [[ -z "${TARGET_REF}" ]]; then
    if [[ -n "${PIN_SHA}" ]]; then
        TARGET_REF="${PIN_SHA}"
    elif [[ -f "${APP_DIR}/.deploy_pin" ]]; then
        TARGET_REF=$(grep -E '^[0-9a-fA-F]{7,40}$' "${APP_DIR}/.deploy_pin" \
            | head -1 || true)
        if [[ -z "${TARGET_REF}" ]]; then
            TARGET_REF="origin/${BRANCH}"
        fi
    else
        TARGET_REF="origin/${BRANCH}"
    fi
fi

echo "    [git] target = ${TARGET_REF}"

# Если у нас pinned SHA — shallow fetch не подходит (объект может быть
# старее границы depth=1). Без depth тянем гарантированно.
USE_SHALLOW=1
if [[ "${TARGET_REF}" != origin/* ]]; then
    USE_SHALLOW=0
fi

if [[ "${USE_SHALLOW}" -eq 1 ]]; then
    if ! git fetch --depth=1 origin "${BRANCH}"; then
        echo "    [git] fetch --depth=1 упал, пробую без --depth"
        if ! git fetch --tags --prune origin "${BRANCH}"; then
            echo "    [git] fetch упал — нет доступа к remote"
            exit 1
        fi
    fi
else
    # При pinned SHA берём всё с тегами, чтобы найти любой объект.
    if ! git fetch --tags --prune origin; then
        echo "    [git] fetch (full) упал — нет доступа к remote"
        exit 1
    fi
fi

# reset --hard перезаписывает tracked файлы. non-tracked не трогает.
if ! git reset --hard "${TARGET_REF}"; then
    echo "    [git] reset --hard ${TARGET_REF} упал"
    exit 1
fi

# git clean удалит tracked-файлы, которых больше нет в репо, но
# НЕ трогает .venv, data, .env, logs (они non-tracked).
git clean -fd --exclude='.venv' --exclude='data' --exclude='.env' \
    --exclude='logs' --exclude='BUILD_INFO' --exclude='backups' \
    --exclude='.deploy_pin' 2>/dev/null || true

# Обязательные runtime-папки. systemd-сервис стартует с
# ProtectHome/InaccessiblePaths, который требует, чтобы logs/ и
# data/ существовали ДО старта. Без них unit падает с
# "Failed to set up mount namespacing: ... No such file or directory".
mkdir -p "${APP_DIR}/logs" "${APP_DIR}/data"

echo "    [git] fetch+reset на ${TARGET_REF}: OK"

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
        echo "target_ref=${TARGET_REF}"
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

echo "    Готово (target=${TARGET_REF})."
exit 0
