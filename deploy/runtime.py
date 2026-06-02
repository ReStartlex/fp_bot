"""
Безопасные операции вокруг update.sh / fetch_code.sh.

Зачем отдельный модуль на Python вместо чистого bash:
- логику бэкапа с ротацией и резолва pinned-коммита удобнее держать в
  тестируемом коде, а не в трудночитаемых find -printf конструкциях;
- bash-скрипты остаются тонкими: они только дёргают этот модуль;
- модуль не импортирует ничего из ``src/`` — он обязан работать ещё
  до того, как код приложения распакован/обновлён.

CLI:

    python -m deploy.runtime backup --app-dir /opt/funpay-ns-bot --keep 10
    python -m deploy.runtime resolve-target --app-dir /opt/funpay-ns-bot \\
        [--default-branch main]
    python -m deploy.runtime verify-staging --dir /opt/funpay-ns-bot.staging

Все подкоманды печатают понятную человеку строку в stdout и завершаются
ненулевым кодом только при настоящих ошибках. Не критичные warning-и
(нет .env, нет БД — это нормально на первом деплое) идут в stderr.
"""
from __future__ import annotations

import argparse
import compileall
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# Файлы, которые мы обязаны защитить перед каждым обновлением.
# Хранятся вне git, восстановление вручную из BACKUP_DIR.
BACKUP_TARGETS: tuple[str, ...] = (
    ".env",
    "data/bridge.db",
    "data/bridge.db-wal",
    "data/bridge.db-shm",
)

BACKUP_DIR_NAME = "backups"
DEFAULT_KEEP = 10

# Pin-файл лежит рядом с приложением. Если он есть — fetch_code.sh
# обязан reset'нуться ровно на этот SHA, игнорируя origin/main.
PIN_FILENAME = ".deploy_pin"

# Распознаём как валидный full/short git SHA. Не пытаемся принимать
# имена веток — pin это именно жёсткая привязка к коммиту.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class BackupResult:
    backup_dir: Path
    copied: tuple[Path, ...]
    skipped: tuple[Path, ...]
    pruned: tuple[Path, ...]

    def summary_line(self) -> str:
        return (
            f"backup_dir={self.backup_dir} "
            f"copied={len(self.copied)} "
            f"skipped={len(self.skipped)} "
            f"pruned={len(self.pruned)}"
        )


def _timestamp(now: datetime | None = None) -> str:
    """Сортируемый UTC-таймстемп для имени директории бэкапа."""
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y%m%d-%H%M%S")


def make_backup(
    app_dir: Path,
    *,
    keep: int = DEFAULT_KEEP,
    now: datetime | None = None,
    targets: tuple[str, ...] = BACKUP_TARGETS,
) -> BackupResult:
    """
    Сделать снимок критичных файлов перед обновлением.

    - Копирует существующие из `targets` в ``<app_dir>/backups/<ts>/``.
    - Если файла нет — не падаем (logs в stderr), просто пропускаем.
    - После копирования удаляем все бэкапы старше `keep`-го по времени.

    Бэкап делается ДО `systemctl stop`, чтобы SQLite WAL/SHM ещё
    отражали последнее консистентное состояние. Это даёт нам право
    откатить ровно ту БД, с которой бот только что работал.

    Возвращает структуру с путями и счётчиками — её удобно печатать
    в логе update.sh и проверять из тестов.
    """
    if keep < 1:
        raise ValueError(f"keep должен быть >= 1, получено {keep}")
    app_dir = app_dir.resolve()
    if not app_dir.exists():
        raise FileNotFoundError(f"APP_DIR не существует: {app_dir}")

    backups_root = app_dir / BACKUP_DIR_NAME
    backups_root.mkdir(parents=True, exist_ok=True)

    ts = _timestamp(now)
    backup_dir = backups_root / ts
    # Если кто-то запустил бэкап два раза в ту же секунду — добавим суффикс.
    suffix = 1
    while backup_dir.exists():
        backup_dir = backups_root / f"{ts}-{suffix}"
        suffix += 1
    backup_dir.mkdir(parents=True)

    copied: list[Path] = []
    skipped: list[Path] = []
    for rel in targets:
        src = app_dir / rel
        if not src.exists():
            skipped.append(src)
            continue
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(dst)

    pruned = _prune_old_backups(backups_root, keep=keep)
    return BackupResult(
        backup_dir=backup_dir,
        copied=tuple(copied),
        skipped=tuple(skipped),
        pruned=tuple(pruned),
    )


def _prune_old_backups(backups_root: Path, *, keep: int) -> list[Path]:
    """Удалить старые директории бэкапов, оставив `keep` самых свежих."""
    if not backups_root.exists():
        return []
    entries = [p for p in backups_root.iterdir() if p.is_dir()]
    # Сортируем по mtime: имена с одинаковым префиксом по дате,
    # mtime даёт стабильный «свежий → старый» порядок и нечувствителен
    # к ручным переименованиям.
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    to_remove = entries[keep:]
    pruned: list[Path] = []
    for path in to_remove:
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            pruned.append(path)
    return pruned


def resolve_target_ref(
    app_dir: Path,
    *,
    env_pin: str | None = None,
    default_branch: str = "main",
) -> str:
    """
    Решить, на какой git-объект делать ``git reset --hard``.

    Приоритет:
    1. ``env_pin`` (обычно прокидывается из env-var ``PIN_SHA``) — если
       это валидный SHA, возвращается как есть.
    2. Файл ``<app_dir>/.deploy_pin`` — первая непустая строка,
       не начинающаяся с ``#``. Должна быть валидным SHA.
    3. Fallback — ``origin/<default_branch>`` (обычно ``origin/main``).

    Любая попытка подсунуть «main» или «v1.0» через pin отвергается:
    pin — это именно защита от случайного отката на «плохой» коммит, и
    она должна указывать на конкретный SHA.
    """
    if env_pin is not None and env_pin.strip():
        candidate = env_pin.strip()
        if not _SHA_RE.match(candidate):
            raise ValueError(
                f"PIN_SHA должен быть git SHA (7..40 hex chars), "
                f"получено {candidate!r}"
            )
        return candidate

    pin_file = app_dir / PIN_FILENAME
    if pin_file.exists():
        for raw_line in pin_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if not _SHA_RE.match(line):
                raise ValueError(
                    f"{pin_file}: первая значимая строка должна быть git SHA, "
                    f"получено {line!r}"
                )
            return line

    return f"origin/{default_branch}"


# ────────────────── verify_staging ──────────────────


# Минимальный набор файлов, без которого деплой бессмысленно swap'ать
# в production. Если хоть одного нет — fetch явно сломался (например,
# tarball оборвался посередине, или git fetch упал на странице ошибки).
REQUIRED_STAGING_FILES: tuple[str, ...] = (
    "src/_version.py",
    "src/main.py",
    "requirements.txt",
)

# Минимальный размер _version.py. Если меньше — это явно не наш файл
# (он содержит как минимум docstring + 3 строки SHA/DATE/SUBJECT).
_MIN_VERSION_PY_SIZE = 80


@dataclass(frozen=True)
class StagingVerifyResult:
    ok: bool
    errors: tuple[str, ...]
    checked_files: int
    compiled_modules: int

    def summary_line(self) -> str:
        status = "OK" if self.ok else "FAIL"
        return (
            f"verify_staging={status} "
            f"checked={self.checked_files} "
            f"compiled={self.compiled_modules} "
            f"errors={len(self.errors)}"
        )


def verify_staging(staging_dir: Path) -> StagingVerifyResult:
    """
    Проверить, что staging-папка пригодна для swap'a в production.

    Что проверяем:
      1. Каталог существует.
      2. Все файлы из `REQUIRED_STAGING_FILES` есть и непустые.
      3. `src/_version.py` содержит как минимум SHA= и SUBJECT=
         (защита от случая, когда fetch вернул HTML страницу ошибки
         GitHub'а, которая просто записалась в файл с тем же именем).
      4. `compileall src/` проходит без SyntaxError — гарантирует,
         что код хотя бы парсится. Это самый быстрый smoke-test,
         не требует .venv и не тратит время на импорт зависимостей.

    Возвращает `StagingVerifyResult.ok=True` ТОЛЬКО если все проверки
    зелёные. На False — update.sh обязан `exit 1` БЕЗ остановки
    production-сервиса (бот продолжает работать на старом коде).
    """
    staging_dir = staging_dir.resolve()
    errors: list[str] = []
    checked = 0

    if not staging_dir.is_dir():
        return StagingVerifyResult(
            ok=False,
            errors=(f"staging dir not found: {staging_dir}",),
            checked_files=0,
            compiled_modules=0,
        )

    for rel in REQUIRED_STAGING_FILES:
        path = staging_dir / rel
        checked += 1
        if not path.is_file():
            errors.append(f"missing required file: {rel}")
            continue
        size = path.stat().st_size
        if size == 0:
            errors.append(f"empty file: {rel}")
            continue
        if rel == "src/_version.py":
            if size < _MIN_VERSION_PY_SIZE:
                errors.append(
                    f"{rel} suspiciously small ({size} bytes < "
                    f"{_MIN_VERSION_PY_SIZE})"
                )
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if "SHA" not in content or "SUBJECT" not in content:
                errors.append(
                    f"{rel} doesn't look like our _version.py "
                    f"(no SHA/SUBJECT markers)"
                )

    # compileall src/: ловит SyntaxError. Падать только на синтаксе,
    # а не на ImportError (последний возможен из-за отсутствия .venv
    # в staging — это норма, имеют значение только синтаксические сбои).
    compiled = 0
    src_dir = staging_dir / "src"
    if src_dir.is_dir():
        compiled = sum(1 for _ in src_dir.rglob("*.py"))
        ok = compileall.compile_dir(
            str(src_dir),
            quiet=2,
            force=True,
            workers=0,
        )
        if not ok:
            errors.append("compileall(src/) found SyntaxError")
    else:
        errors.append("staging has no src/ directory")

    return StagingVerifyResult(
        ok=not errors,
        errors=tuple(errors),
        checked_files=checked,
        compiled_modules=compiled,
    )


# ----------------------------- CLI -----------------------------


def _cmd_backup(args: argparse.Namespace) -> int:
    app_dir = Path(args.app_dir)
    try:
        result = make_backup(app_dir, keep=args.keep)
    except Exception as exc:
        print(f"backup FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if result.skipped:
        for p in result.skipped:
            print(f"backup skip (нет файла): {p}", file=sys.stderr)
    if result.pruned:
        for p in result.pruned:
            print(f"backup pruned (старее keep={args.keep}): {p}", file=sys.stderr)
    print(result.summary_line())
    print(f"BACKUP_DIR={result.backup_dir}")
    return 0


def _cmd_resolve_target(args: argparse.Namespace) -> int:
    import os

    app_dir = Path(args.app_dir)
    env_pin = os.environ.get("PIN_SHA") if not args.no_env else None
    try:
        target = resolve_target_ref(
            app_dir,
            env_pin=env_pin,
            default_branch=args.default_branch,
        )
    except Exception as exc:
        print(
            f"resolve-target FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(target)
    return 0


def _cmd_verify_staging(args: argparse.Namespace) -> int:
    staging_dir = Path(args.dir)
    try:
        result = verify_staging(staging_dir)
    except Exception as exc:
        print(
            f"verify-staging FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    for err in result.errors:
        print(f"verify-staging error: {err}", file=sys.stderr)
    print(result.summary_line())
    return 0 if result.ok else 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy.runtime",
        description="Безопасные операции для deploy-скриптов.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backup = sub.add_parser(
        "backup",
        help="Сделать бэкап .env + bridge.db перед обновлением.",
    )
    p_backup.add_argument("--app-dir", required=True, help="Каталог приложения (APP_DIR).")
    p_backup.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"Сколько последних бэкапов оставить (по умолчанию {DEFAULT_KEEP}).",
    )
    p_backup.set_defaults(func=_cmd_backup)

    p_resolve = sub.add_parser(
        "resolve-target",
        help="Вывести git-ref, на который должен reset'иться fetch_code.sh.",
    )
    p_resolve.add_argument("--app-dir", required=True)
    p_resolve.add_argument("--default-branch", default="main")
    p_resolve.add_argument(
        "--no-env",
        action="store_true",
        help="Игнорировать переменную окружения PIN_SHA (для тестов).",
    )
    p_resolve.set_defaults(func=_cmd_resolve_target)

    p_verify = sub.add_parser(
        "verify-staging",
        help=(
            "Проверить staging-папку перед swap'ом в production. "
            "Возвращает 0 если staging пригоден, 3 если не пригоден "
            "(update.sh должен на 3 завершиться БЕЗ остановки сервиса), "
            "2 на непредвиденной ошибке."
        ),
    )
    p_verify.add_argument(
        "--dir", required=True, help="Путь к staging-каталогу для проверки."
    )
    p_verify.set_defaults(func=_cmd_verify_staging)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
