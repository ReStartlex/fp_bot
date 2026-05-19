"""
Записывает src/_version.py из текущего git HEAD.

Запускается перед `git push` (вручную или из pre-push хука):

    python deploy/stamp_version.py && git add src/_version.py \
        && git commit --amend --no-edit && git push

Цель — чтобы на VPS в /version и в выводе update.sh всегда был
актуальный SHA и сообщение коммита, БЕЗ зависимости от api.github.com
(который через прокси у нас стабильно отдаёт 403).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(*args: str) -> str:
    out = subprocess.check_output(["git", *args], text=True).strip()
    return out


def main() -> int:
    try:
        sha = _git("rev-parse", "HEAD")
        date = _git("show", "-s", "--format=%cI", "HEAD")
        subject = _git("show", "-s", "--format=%s", "HEAD")
    except subprocess.CalledProcessError as exc:
        print(f"git failed: {exc}", file=sys.stderr)
        return 1

    subject = subject.replace('"', '\\"')[:200]

    body = (
        '"""\n'
        "Версия задеплоенного кода. Записывается автоматически скриптом\n"
        "deploy/stamp_version.py перед каждым push'ем.\n\n"
        "ВАЖНО: НЕ редактируй вручную — твои изменения будут перезаписаны.\n"
        '"""\n'
        f'SHA = "{sha}"\n'
        f'DATE = "{date}"\n'
        f'SUBJECT = "{subject}"\n'
    )
    path = Path(__file__).resolve().parent.parent / "src" / "_version.py"
    path.write_text(body, encoding="utf-8")
    print(f"src/_version.py updated -> {sha[:12]}  {date}  {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
