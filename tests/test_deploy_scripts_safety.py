"""
Статические тесты на deploy/update.sh и deploy/fetch_code.sh.

Здесь мы НЕ запускаем bash на CI (репо тестируется и на Windows-машинах),
а проверяем, что в скриптах присутствуют ключевые опорные точки: бэкап
перед апдейтом, поддержка PIN_SHA и GIT_HTTP_PROXY, инструкция по откату.
Логика выполнения отдельно покрыта в tests/test_deploy_runtime.py.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = ROOT / "deploy" / "update.sh"
FETCH_SH = ROOT / "deploy" / "fetch_code.sh"


def test_update_runs_backup_before_stopping_service():
    """Бэкап должен быть ДО systemctl stop, чтобы БД и .env были консистентны."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    backup_pos = text.find("-m deploy.runtime backup")
    stop_pos = text.find("systemctl stop funpay-ns-bot")
    assert backup_pos != -1, "update.sh должен вызывать deploy.runtime backup"
    assert stop_pos != -1, "update.sh должен останавливать funpay-ns-bot"
    assert backup_pos < stop_pos, (
        "бэкап должен делаться ДО systemctl stop, иначе БД может быть в неконсистентном состоянии"
    )


def test_update_prints_rollback_hint_on_failure():
    text = UPDATE_SH.read_text(encoding="utf-8")
    # Hint должен срабатывать в обеих ветках (bot и api), и упоминать
    # ключевые шаги: cp .env, cp bridge.db, PIN_SHA.
    assert "_print_rollback_hint" in text
    assert "cp '${hint_backup}/.env'" in text
    assert "cp '${hint_backup}/data/bridge.db'" in text
    assert "PIN_SHA=<sha_прошлой_рабочей_версии>" in text


def test_update_keeps_backup_setting_configurable():
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "BACKUP_KEEP=${BACKUP_KEEP:-10}" in text


def test_update_documents_pin_and_proxy_env_vars():
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "PIN_SHA=<sha>" in text
    assert "GIT_HTTP_PROXY=<url>" in text


def test_fetch_resolves_target_via_runtime():
    text = FETCH_SH.read_text(encoding="utf-8")
    assert "deploy.runtime resolve-target" in text
    # CLI-вызов должен пробрасывать APP_DIR и default-branch
    assert "--app-dir \"${APP_DIR}\"" in text
    assert "--default-branch \"${BRANCH}\"" in text


def test_fetch_has_python_free_fallback_for_pin():
    """На самом первом bootstrap'е .venv ещё нет — должен работать без Python."""
    text = FETCH_SH.read_text(encoding="utf-8")
    # Простой grep по SHA в .deploy_pin как fallback, без deploy.runtime
    assert 'grep -E \'^[0-9a-fA-F]{7,40}$\' "${APP_DIR}/.deploy_pin"' in text
    assert 'TARGET_REF="${PIN_SHA}"' in text


def test_fetch_supports_http_proxy_with_cleanup():
    text = FETCH_SH.read_text(encoding="utf-8")
    assert "GIT_HTTP_PROXY=${GIT_HTTP_PROXY:-}" in text
    assert 'git config --global http.proxy  "${GIT_HTTP_PROXY}"' in text
    assert 'git config --global https.proxy "${GIT_HTTP_PROXY}"' in text
    # cleanup ОБЯЗАН срабатывать на exit, иначе прокси останется висеть
    # в системном git config и сломает обычный apt update.
    assert "trap _cleanup_proxy EXIT" in text
    assert "git config --global --unset http.proxy" in text


def test_fetch_full_clone_when_target_is_pinned_sha():
    """При pinned SHA --depth=1 может не дотянуться до старого объекта."""
    text = FETCH_SH.read_text(encoding="utf-8")
    # Должен быть явный режим без --depth для pinned target.
    assert 'USE_SHALLOW=0' in text
    assert 'git fetch --tags --prune origin' in text


def test_fetch_clean_preserves_backups_and_pin():
    text = FETCH_SH.read_text(encoding="utf-8")
    # backups/ и .deploy_pin создаются вне git и должны переживать reset.
    assert "--exclude='backups'" in text
    assert "--exclude='.deploy_pin'" in text


def test_fetch_writes_build_info_with_target_ref():
    text = FETCH_SH.read_text(encoding="utf-8")
    assert 'target_ref=${TARGET_REF}' in text


def test_readme_documents_pin_sha_and_backup_flow():
    docs = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "PIN_SHA" in docs
    assert "GIT_HTTP_PROXY" in docs
    assert ".deploy_pin" in docs
    assert "backups/<timestamp>" in docs
    assert "Если сервис не поднялся" in docs


def test_repo_pins_unix_line_endings_for_shell_scripts():
    """LF для *.sh обязателен, иначе bash на Linux падает с 'bad interpreter: \\r'."""
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh" in attrs and "eol=lf" in attrs
