"""
Статические тесты на deploy/update.sh и deploy/fetch_code.sh.

Здесь мы НЕ запускаем bash на CI (репо тестируется и на Windows-машинах),
а проверяем, что в скриптах присутствуют ключевые опорные точки: бэкап
перед апдейтом, поддержка PIN_SHA и GIT_HTTP_PROXY, инструкция по откату,
fetch+verify-then-stop-then-swap последовательность.
Логика выполнения отдельно покрыта в tests/test_deploy_runtime.py.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = ROOT / "deploy" / "update.sh"
FETCH_SH = ROOT / "deploy" / "fetch_code.sh"
GITIGNORE = ROOT / ".gitignore"


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
    # .deploy_pin лежит в production-каталоге, а не в staging. fetch_code.sh
    # вызывается с APP_DIR=staging, поэтому resolve-target должен смотреть
    # в PROD_APP_DIR (см. ниже test_fetch_reads_deploy_pin_from_prod_dir).
    assert "--app-dir \"${PROD_APP_DIR}\"" in text
    assert "--default-branch \"${BRANCH}\"" in text


def test_fetch_has_python_free_fallback_for_pin():
    """На самом первом bootstrap'е .venv ещё нет — должен работать без Python.

    .deploy_pin читается из production-каталога, не из staging."""
    text = FETCH_SH.read_text(encoding="utf-8")
    assert 'grep -E \'^[0-9a-fA-F]{7,40}$\' "${PROD_APP_DIR}/.deploy_pin"' in text
    assert 'TARGET_REF="${PIN_SHA}"' in text


def test_fetch_reads_deploy_pin_from_prod_dir_not_staging():
    """
    Главный фикс: при staging-деплое (APP_DIR=/opt/funpay-ns-bot.staging)
    .deploy_pin живёт в production (/opt/funpay-ns-bot/.deploy_pin),
    в staging его нет. fetch_code.sh должен явно знать про PROD_APP_DIR,
    с дефолтом = APP_DIR (для случая когда нет staging).
    """
    text = FETCH_SH.read_text(encoding="utf-8")
    assert "PROD_APP_DIR=${PROD_APP_DIR:-${APP_DIR}}" in text, (
        "fetch_code.sh должен принимать PROD_APP_DIR из env, по умолчанию = APP_DIR"
    )


def test_fetch_supports_http_proxy_via_per_command_override():
    """
    Прокси настраивается через `git -c http.proxy=...` per-команда,
    а не через `git config --global`. Это критично потому что:
      1) global-config переживает скрипт и протекает в apt/curl;
      2) если global уже грязный (другой процесс прописал прокси),
         наш `--unset` чистит чужие настройки.
    """
    text = FETCH_SH.read_text(encoding="utf-8")
    assert "GIT_HTTP_PROXY=${GIT_HTTP_PROXY:-}" in text
    # НИ ОДНОГО global-config-set proxy в скрипте быть не должно.
    assert 'git config --global http.proxy' not in text, (
        "Не используем git config --global для прокси — этот state протекает наружу"
    )
    assert 'git config --global https.proxy' not in text


def test_fetch_clears_local_proxy_on_start():
    """
    Локальный $APP_DIR/.git/config мог остаться грязным от прошлого
    запуска (или от рук в инциденте). На старте — снимаем local proxy,
    это наш каталог, безопасно.
    """
    text = FETCH_SH.read_text(encoding="utf-8")
    assert "git config --local --unset-all http.proxy" in text
    assert "git config --local --unset-all https.proxy" in text


def test_fetch_uses_per_command_proxy_args_in_git_fetch():
    """git fetch выполняется с -c http.proxy=... -c https.proxy=...
    (per-command), а не полагается на global/local state."""
    text = FETCH_SH.read_text(encoding="utf-8")
    # Сборка git-args массива
    assert "GIT_ARGS=(" in text or "GIT_ARGS=()" in text, (
        "fetch_code.sh должен собирать массив GIT_ARGS для per-command-конфига"
    )
    # И использовать его при fetch
    assert 'git "${GIT_ARGS[@]}" fetch' in text, (
        "git fetch должен идти с GIT_ARGS (per-command config), "
        "иначе global-настройки могут протечь"
    )


def test_fetch_explicit_empty_proxy_when_no_http_proxy_set():
    """
    Когда GIT_HTTP_PROXY не задан и мы идём через gh-proxy.com напрямую,
    унаследованные HTTP_PROXY/HTTPS_PROXY/ALL_PROXY env-переменные
    могут сломать fetch (libcurl их подхватит). Защищаемся per-command:
    -c http.proxy= -c https.proxy= (пустые) явно отключают.
    """
    text = FETCH_SH.read_text(encoding="utf-8")
    assert '"http.proxy="' in text, (
        "При пустом GIT_HTTP_PROXY git должен получить -c http.proxy= "
        "(пустое значение) — это явно отключает proxy на уровне команды, "
        "перебивая HTTP_PROXY env-vars"
    )
    assert '"https.proxy="' in text


def test_fetch_full_clone_when_target_is_pinned_sha():
    """При pinned SHA --depth=1 может не дотянуться до старого объекта."""
    text = FETCH_SH.read_text(encoding="utf-8")
    # Должен быть явный режим без --depth для pinned target.
    assert 'USE_SHALLOW=0' in text
    # git fetch теперь идёт с GIT_ARGS (per-command proxy override).
    assert 'git "${GIT_ARGS[@]}" fetch --tags --prune origin' in text


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


def test_readme_documents_staging_strategy():
    """README должен явно объяснять fetch-then-verify-then-stop стратегию,
    чтобы оператор понимал, что fetch-сбой больше не убивает прод."""
    docs = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "STAGING_DIR" in docs
    assert "verify-staging" in docs
    assert "БЕЗ остановки сервиса" in docs
    assert "23.05.2026" in docs, (
        "README должен напоминать про инцидент, чтобы было понятно, "
        "зачем нужна именно такая последовательность"
    )


def test_repo_pins_unix_line_endings_for_shell_scripts():
    """LF для *.sh обязателен, иначе bash на Linux падает с 'bad interpreter: \\r'."""
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh" in attrs and "eol=lf" in attrs


# ─────────────── staging-стратегия (fetch+verify до stop) ───────────────


def test_update_fetches_to_staging_before_stopping_service():
    """fetch_code.sh должен дёргаться с APP_DIR=staging ДО systemctl stop.

    Это главный фикс инцидента 23.05.2026: тогда update.sh останавливал
    сервис ПЕРЕД fetch'ем, и при сетевом сбое прод оставался лежать.
    Теперь fetch идёт в staging, бот продолжает работать; stop —
    только после успешного fetch+verify.
    """
    text = UPDATE_SH.read_text(encoding="utf-8")
    fetch_pos = text.find('bash "${APP_DIR}/deploy/fetch_code.sh"')
    stop_pos = text.find("systemctl stop funpay-ns-bot")
    assert fetch_pos != -1, (
        "update.sh должен вызывать fetch_code.sh"
    )
    assert stop_pos != -1, "update.sh должен останавливать funpay-ns-bot"
    assert fetch_pos < stop_pos, (
        "fetch_code.sh (в staging) ОБЯЗАН вызываться ДО systemctl stop, "
        "иначе сетевой сбой fetch оставит прод без работы"
    )
    # Конкретные env-vars: STAGING — целевой каталог, PROD_APP_DIR —
    # каталог для чтения .deploy_pin.
    fetch_line_end = text.find("\n", fetch_pos)
    fetch_line_start = text.rfind("\n", 0, fetch_pos) + 1
    fetch_invocation = text[fetch_line_start:fetch_line_end]
    assert 'APP_DIR="${STAGING_DIR}"' in fetch_invocation, (
        "fetch_code.sh должен вызываться с APP_DIR=STAGING_DIR"
    )
    assert 'PROD_APP_DIR="${APP_DIR}"' in fetch_invocation, (
        "update.sh должен прокидывать PROD_APP_DIR=production-каталог, "
        "иначе fetch_code.sh не найдёт .deploy_pin (он лежит в production, "
        "не в staging)"
    )


def test_update_verifies_staging_before_stopping_service():
    """verify-staging обязан вызываться ДО systemctl stop.

    Без verify плохой fetch (HTML вместо tarball, оборванный архив)
    мог бы пройти rsync'ом и сломать прод. Verify — последний барьер
    перед swap'ом.
    """
    text = UPDATE_SH.read_text(encoding="utf-8")
    verify_pos = text.find("deploy.runtime verify-staging")
    stop_pos = text.find("systemctl stop funpay-ns-bot")
    assert verify_pos != -1, "update.sh должен вызывать deploy.runtime verify-staging"
    assert stop_pos != -1
    assert verify_pos < stop_pos, (
        "verify-staging ОБЯЗАН вызываться ДО systemctl stop"
    )


def test_update_exits_without_stop_on_fetch_failure():
    """Если fetch упал — exit 1 БЕЗ остановки сервиса.
    Текст ошибки должен явно говорить, что бот продолжает работать."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    # ищем ветку else после fetch_code.sh — invocation теперь содержит
    # и APP_DIR=STAGING_DIR, и PROD_APP_DIR=APP_DIR.
    fetch_block_start = text.find('bash "${APP_DIR}/deploy/fetch_code.sh"')
    stop_pos = text.find("systemctl stop funpay-ns-bot")
    assert fetch_block_start != -1 and stop_pos != -1

    fetch_block = text[fetch_block_start:stop_pos]
    # в этой части должен быть exit 1 (на упавший fetch)
    assert "exit 1" in fetch_block, "fetch-fail должен делать exit без stop"
    # и явное сообщение про продолжающего работу бота
    assert "продолжает работать" in fetch_block


def test_update_exits_without_stop_on_verify_failure():
    """verify_RC != 0 → exit 1 БЕЗ остановки сервиса."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    verify_pos = text.find("deploy.runtime verify-staging")
    stop_pos = text.find("systemctl stop funpay-ns-bot")
    assert verify_pos != -1 and stop_pos != -1

    verify_block = text[verify_pos:stop_pos]
    assert "exit 1" in verify_block, "verify-fail должен делать exit без stop"
    # явная индикация, что сервис не остановлен
    assert "продолжает работать" in verify_block or "НЕ был остановлен" in verify_block


def test_update_swaps_via_rsync_with_exclusions():
    """rsync staging → production с правильными exclude'ами, иначе
    мы рискуем затереть .env / data/ / .venv."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert 'rsync -a --delete' in text
    for excl in ("'.env'", "'data/'", "'logs/'", "'backups/'",
                 "'.venv/'", "'.git/'", "'.deploy_pin'"):
        assert f"--exclude={excl}" in text, f"rsync обязан исключать {excl}"


def test_update_supports_staging_dir_env():
    """STAGING_DIR должна быть конфигурируемой через env."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert 'STAGING_DIR=${STAGING_DIR:-${APP_DIR}.staging}' in text


def test_fetch_code_app_dir_is_configurable():
    """fetch_code.sh должен работать с APP_DIR из env (для staging).

    Раньше было APP_DIR=/opt/funpay-ns-bot жёстко — это не давало
    update.sh использовать staging-каталог.
    """
    text = FETCH_SH.read_text(encoding="utf-8")
    assert 'APP_DIR=${APP_DIR:-/opt/funpay-ns-bot}' in text


# ─────────────── .gitignore ───────────────


def test_gitignore_excludes_backups_directory():
    """backups/ содержит .env и bridge.db — оба с секретами.
    Никогда не должны попасть в git index (даже от случайного git add -A)."""
    text = GITIGNORE.read_text(encoding="utf-8")
    assert "backups/" in text, ".gitignore должен исключать backups/"


def test_gitignore_excludes_local_agent_files():
    """PROJECT_CONTEXT.md и image.png — рабочие файлы агента, не должны коммититься."""
    text = GITIGNORE.read_text(encoding="utf-8")
    assert "PROJECT_CONTEXT.md" in text
    assert "image.png" in text


def test_gitignore_is_clean_utf8_no_utf16_garbage():
    """В прошлом сохранение через PowerShell оставило в хвосте .gitignore
    кусок UTF-16 (`. c o m m i t _ m s g . t m p`). Проверяем, что
    файл — чистый UTF-8 без таких артефактов."""
    raw = GITIGNORE.read_bytes()
    # UTF-16 текст имеет нулевые байты между ASCII-символами.
    # В нормальном UTF-8 .gitignore нулевых байтов не бывает.
    assert b"\x00" not in raw, ".gitignore содержит NUL-байт (UTF-16 артефакт)"
    # Шаблон с пробелами между каждым ASCII-символом — типичный
    # признак UTF-16 текста, прочитанного как UTF-8.
    text = GITIGNORE.read_text(encoding="utf-8")
    assert ". c o m m i t " not in text
