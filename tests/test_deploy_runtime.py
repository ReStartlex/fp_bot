"""Тесты для deploy/runtime.py — бэкапы и резолв pinned-коммита."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deploy.runtime import (
    BACKUP_DIR_NAME,
    BACKUP_TARGETS,
    PIN_FILENAME,
    REQUIRED_STAGING_FILES,
    make_backup,
    resolve_target_ref,
    verify_staging,
)


# ─────────────── фикстуры ───────────────


def _seed_app_dir(tmp_path: Path, *, with_db: bool = True, with_env: bool = True) -> Path:
    """Создаёт правдоподобный APP_DIR с .env и SQLite-БД."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    if with_env:
        (tmp_path / ".env").write_text(
            "FUNPAY_GOLDEN_KEY=test\nNS_API_SECRET=AAA=\n", encoding="utf-8"
        )
    if with_db:
        db_path = tmp_path / "data" / "bridge.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE smoke (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO smoke(value) VALUES ('hello')")
            conn.commit()
        finally:
            conn.close()
    return tmp_path


# ─────────────── make_backup ───────────────


def test_make_backup_copies_env_and_db(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)

    result = make_backup(app_dir, keep=5)

    assert result.backup_dir.parent.name == BACKUP_DIR_NAME
    assert result.backup_dir.exists()

    expected_env = result.backup_dir / ".env"
    expected_db = result.backup_dir / "data" / "bridge.db"
    assert expected_env.read_text(encoding="utf-8").startswith("FUNPAY_GOLDEN_KEY=")
    assert expected_db.exists() and expected_db.stat().st_size > 0

    # WAL/SHM не создавали — должны быть в skipped, не падает.
    skipped_names = {p.name for p in result.skipped}
    assert "bridge.db-wal" in skipped_names
    assert "bridge.db-shm" in skipped_names


def test_make_backup_works_without_env_or_db(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path, with_db=False, with_env=False)

    result = make_backup(app_dir, keep=3)

    # backup_dir создан, copied пустой, skipped содержит все цели.
    assert result.backup_dir.exists()
    assert result.copied == ()
    skipped_rel = {p.relative_to(app_dir).as_posix() for p in result.skipped}
    assert set(BACKUP_TARGETS) == skipped_rel


def test_make_backup_prunes_old_keeping_n(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    backups_root = app_dir / BACKUP_DIR_NAME

    # Создаём 12 бэкапов с искусственно сдвинутыми mtime, чтобы порядок
    # был детерминированным и не зависел от скорости теста.
    moments = [
        datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc).replace(
            minute=i, second=i
        )
        for i in range(12)
    ]
    created: list[Path] = []
    for moment in moments:
        result = make_backup(app_dir, keep=99, now=moment)
        created.append(result.backup_dir)
        # Принудительно ставим mtime, чтобы pruning сортировал предсказуемо.
        ts = moment.timestamp()
        os.utime(result.backup_dir, (ts, ts))

    # Финальный прогон с keep=10 должен оставить 10 самых свежих.
    final = make_backup(app_dir, keep=10, now=datetime(2026, 5, 1, 11, 0, 0, tzinfo=timezone.utc))
    surviving = sorted(p for p in backups_root.iterdir() if p.is_dir())
    assert len(surviving) == 10
    # Самые старые — удалены.
    assert created[0] not in surviving
    assert created[1] not in surviving
    # Самые свежие, и сам final — на месте.
    assert created[-1] in surviving
    assert final.backup_dir in surviving


def test_make_backup_handles_collision_same_second(tmp_path: Path):
    """Два бэкапа в одну секунду должны успешно создаться рядом."""
    app_dir = _seed_app_dir(tmp_path)
    moment = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)

    r1 = make_backup(app_dir, keep=10, now=moment)
    r2 = make_backup(app_dir, keep=10, now=moment)

    assert r1.backup_dir != r2.backup_dir
    assert r1.backup_dir.exists() and r2.backup_dir.exists()


def test_make_backup_rejects_invalid_keep(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    with pytest.raises(ValueError):
        make_backup(app_dir, keep=0)


def test_make_backup_raises_when_app_dir_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        make_backup(tmp_path / "does-not-exist", keep=3)


# ─────────────── resolve_target_ref ───────────────


def test_resolve_target_defaults_to_origin_main(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    assert resolve_target_ref(app_dir) == "origin/main"


def test_resolve_target_uses_env_pin(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    sha = "708ed212aa150f6cc45471ff7bb735da1ef0d010"
    assert resolve_target_ref(app_dir, env_pin=sha) == sha


def test_resolve_target_accepts_short_sha(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    assert resolve_target_ref(app_dir, env_pin="708ed21") == "708ed21"


def test_resolve_target_rejects_branch_name_in_pin(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    with pytest.raises(ValueError):
        resolve_target_ref(app_dir, env_pin="main")
    with pytest.raises(ValueError):
        resolve_target_ref(app_dir, env_pin="v1.0.0")


def test_resolve_target_uses_pin_file(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    sha = "708ed212aa150f6cc45471ff7bb735da1ef0d010"
    (app_dir / PIN_FILENAME).write_text(
        f"# зафиксировано вручную из-за инцидента\n{sha}\n", encoding="utf-8"
    )
    assert resolve_target_ref(app_dir) == sha


def test_resolve_target_env_pin_overrides_file(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    file_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    env_sha = "bbbbbbb"
    (app_dir / PIN_FILENAME).write_text(file_sha, encoding="utf-8")
    assert resolve_target_ref(app_dir, env_pin=env_sha) == env_sha


def test_resolve_target_pin_file_with_invalid_sha_raises(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    (app_dir / PIN_FILENAME).write_text("not-a-sha\n", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_target_ref(app_dir)


def test_resolve_target_empty_env_pin_falls_through(tmp_path: Path):
    """Пустая строка / пробелы в env не должны переопределять fallback."""
    app_dir = _seed_app_dir(tmp_path)
    assert resolve_target_ref(app_dir, env_pin="") == "origin/main"
    assert resolve_target_ref(app_dir, env_pin="   ") == "origin/main"


def test_resolve_target_respects_custom_default_branch(tmp_path: Path):
    app_dir = _seed_app_dir(tmp_path)
    assert (
        resolve_target_ref(app_dir, default_branch="stable")
        == "origin/stable"
    )


# ─────────────── CLI ───────────────


def test_cli_backup_prints_backup_dir(tmp_path: Path, capsys):
    app_dir = _seed_app_dir(tmp_path)
    from deploy.runtime import main

    exit_code = main(["backup", "--app-dir", str(app_dir), "--keep", "5"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "BACKUP_DIR=" in captured.out
    # путь должен лежать в <app_dir>/backups/<ts>/
    backup_line = [
        line for line in captured.out.splitlines() if line.startswith("BACKUP_DIR=")
    ][0]
    backup_path = Path(backup_line.split("=", 1)[1])
    assert backup_path.exists()
    assert (app_dir / BACKUP_DIR_NAME) in backup_path.parents


def test_cli_resolve_target_default(tmp_path: Path, capsys, monkeypatch):
    app_dir = _seed_app_dir(tmp_path)
    monkeypatch.delenv("PIN_SHA", raising=False)
    from deploy.runtime import main

    exit_code = main(["resolve-target", "--app-dir", str(app_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == "origin/main"


def test_cli_resolve_target_with_env_pin(tmp_path: Path, capsys, monkeypatch):
    app_dir = _seed_app_dir(tmp_path)
    sha = "708ed212aa150f6cc45471ff7bb735da1ef0d010"
    monkeypatch.setenv("PIN_SHA", sha)
    from deploy.runtime import main

    exit_code = main(["resolve-target", "--app-dir", str(app_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == sha


def test_cli_resolve_target_no_env_flag_ignores_pin(tmp_path: Path, capsys, monkeypatch):
    app_dir = _seed_app_dir(tmp_path)
    monkeypatch.setenv("PIN_SHA", "708ed21")
    from deploy.runtime import main

    exit_code = main(
        ["resolve-target", "--app-dir", str(app_dir), "--no-env"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == "origin/main"


def test_cli_resolve_target_invalid_pin_exits_nonzero(
    tmp_path: Path, capsys, monkeypatch
):
    app_dir = _seed_app_dir(tmp_path)
    monkeypatch.setenv("PIN_SHA", "main")
    from deploy.runtime import main

    exit_code = main(["resolve-target", "--app-dir", str(app_dir)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "resolve-target FAILED" in captured.err


# ─────────────── verify_staging ───────────────


def _seed_valid_staging(tmp_path: Path) -> Path:
    """Создаёт правдоподобный staging-каталог (минимум для прохождения verify)."""
    staging = tmp_path / "staging"
    (staging / "src").mkdir(parents=True)
    (staging / "src" / "_version.py").write_text(
        '"""docstring."""\n'
        'SHA = "abc1234def56789012345678901234567890abcd"\n'
        'DATE = "2026-05-23T20:00:00+00:00"\n'
        'SUBJECT = "feat: test"\n',
        encoding="utf-8",
    )
    (staging / "src" / "main.py").write_text(
        "def main() -> int:\n    return 0\n", encoding="utf-8"
    )
    (staging / "requirements.txt").write_text("pytest>=7\n", encoding="utf-8")
    return staging


def test_verify_staging_ok_on_valid_dir(tmp_path: Path):
    staging = _seed_valid_staging(tmp_path)

    result = verify_staging(staging)

    assert result.ok is True
    assert result.errors == ()
    assert result.checked_files == len(REQUIRED_STAGING_FILES)
    assert result.compiled_modules >= 2  # _version.py + main.py


def test_verify_staging_fails_when_dir_missing(tmp_path: Path):
    result = verify_staging(tmp_path / "nope")

    assert result.ok is False
    assert any("not found" in e for e in result.errors)


def test_verify_staging_fails_on_missing_main_py(tmp_path: Path):
    staging = _seed_valid_staging(tmp_path)
    (staging / "src" / "main.py").unlink()

    result = verify_staging(staging)

    assert result.ok is False
    assert any("missing required file: src/main.py" in e for e in result.errors)


def test_verify_staging_fails_on_empty_version_py(tmp_path: Path):
    staging = _seed_valid_staging(tmp_path)
    (staging / "src" / "_version.py").write_text("", encoding="utf-8")

    result = verify_staging(staging)

    assert result.ok is False
    assert any("empty file: src/_version.py" in e for e in result.errors)


def test_verify_staging_fails_when_version_py_is_html(tmp_path: Path):
    """Защита от ситуации, когда fetch вернул HTML страницу ошибки
    GitHub'а вместо нашего _version.py — без SHA/SUBJECT-маркеров."""
    staging = _seed_valid_staging(tmp_path)
    (staging / "src" / "_version.py").write_text(
        "<html><body>500 Server Error</body></html>"
        + "\n" * 5 + "x" * 100,  # сделаем большим, чтобы не сработал size-check
        encoding="utf-8",
    )

    result = verify_staging(staging)

    assert result.ok is False
    assert any("doesn't look like our _version.py" in e for e in result.errors)


def test_verify_staging_fails_on_syntax_error_in_src(tmp_path: Path):
    """compileall должен поймать сломанный .py в src/."""
    staging = _seed_valid_staging(tmp_path)
    (staging / "src" / "broken.py").write_text(
        "def oops(:\n    pass\n", encoding="utf-8"
    )

    result = verify_staging(staging)

    assert result.ok is False
    assert any("SyntaxError" in e for e in result.errors)


def test_verify_staging_fails_when_no_src_dir(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    # Создадим только requirements.txt, без src/
    (staging / "requirements.txt").write_text("x\n", encoding="utf-8")

    result = verify_staging(staging)

    assert result.ok is False
    # Хотя бы один из ожидаемых маркеров ошибки.
    msg = " | ".join(result.errors)
    assert ("has no src/ directory" in msg) or ("missing required file" in msg)


def test_verify_staging_doesnt_fail_on_runtime_imports(tmp_path: Path):
    """compileall проверяет ТОЛЬКО синтаксис: ImportError (отсутствие
    .venv с зависимостями) не должен валить verify. Это критично, потому
    что в staging-папке нет .venv."""
    staging = _seed_valid_staging(tmp_path)
    (staging / "src" / "uses_missing_dep.py").write_text(
        "from nonexistent_package import boom\n", encoding="utf-8"
    )

    result = verify_staging(staging)

    assert result.ok is True, f"ожидался OK, errors={result.errors}"


# ─────────────── CLI verify-staging ───────────────


def test_cli_verify_staging_ok_returns_zero(tmp_path: Path, capsys):
    staging = _seed_valid_staging(tmp_path)
    from deploy.runtime import main

    exit_code = main(["verify-staging", "--dir", str(staging)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "verify_staging=OK" in captured.out


def test_cli_verify_staging_fail_returns_three(tmp_path: Path, capsys):
    """CLI exit code 3 на невалидной staging — update.sh ловит именно его,
    чтобы отличить от exit 1 (общая ошибка bash) и exit 2 (исключение
    Python). На 3 — НЕ останавливать сервис, оставить старый код."""
    staging = tmp_path / "staging"
    staging.mkdir()
    from deploy.runtime import main

    exit_code = main(["verify-staging", "--dir", str(staging)])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "verify_staging=FAIL" in captured.out
    assert "verify-staging error" in captured.err
