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

Оба подкоманды печатают понятную человеку строку в stdout и завершаются
ненулевым кодом только при настоящих ошибках. Не критичные warning-и
(нет .env, нет БД — это нормально на первом деплое) идут в stderr.
"""
from __future__ import annotations

import argparse
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
