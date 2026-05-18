"""
Разведка установленной версии FunPayAPI: какие методы и атрибуты доступны
на Account и UserProfile, как извлекаются лоты и баланс.

Создаёт читаемый дамп в `data/funpay_introspect.txt`. Этот файл удобно
прислать разработчику для адаптации клиента под конкретную версию либы.

Запуск:
    cd /opt/funpay-ns-bot
    sudo -u bot .venv/bin/python -m src.tools.funpay_introspect
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.config import get_settings
from src.funpay.client import FunPayClient
from src.logging_setup import setup_logging


def _safe_call(fn, *args, **kwargs) -> tuple[Any, str | None]:
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _describe_object(obj: Any, *, name: str, max_attrs: int = 200) -> list[str]:
    """Список строк описания атрибутов и методов объекта."""
    out: list[str] = [f"=== {name} ({type(obj).__module__}.{type(obj).__name__}) ==="]
    if obj is None:
        out.append("  (None)")
        return out
    attrs = sorted(a for a in dir(obj) if not a.startswith("_"))[:max_attrs]
    for attr in attrs:
        try:
            value = getattr(obj, attr)
        except Exception as exc:
            out.append(f"  {attr}: <ошибка чтения: {exc}>")
            continue
        if callable(value):
            try:
                sig = str(inspect.signature(value))
            except (ValueError, TypeError):
                sig = "(?)"
            out.append(f"  {attr}{sig}  <method>")
        else:
            type_name = type(value).__name__
            preview = repr(value)
            if len(preview) > 120:
                preview = preview[:120] + "..."
            out.append(f"  {attr}: {type_name} = {preview}")
    return out


def _try_methods(obj: Any, methods: list[str]) -> list[str]:
    """Пробует вызвать методы без аргументов и возвращает результат."""
    out: list[str] = []
    for m in methods:
        attr = getattr(obj, m, None)
        if attr is None:
            out.append(f"  {m}: NOT_FOUND")
            continue
        if not callable(attr):
            out.append(f"  {m}: ATTR = {type(attr).__name__} {repr(attr)[:120]}")
            continue
        result, err = _safe_call(attr)
        if err:
            out.append(f"  {m}(): ERROR {err}")
            continue
        if result is None:
            out.append(f"  {m}(): -> None")
            continue
        if isinstance(result, (list, tuple)):
            preview = ", ".join(repr(x)[:60] for x in list(result)[:3])
            out.append(f"  {m}(): -> {type(result).__name__}[{len(result)}] [{preview}{'...' if len(result) > 3 else ''}]")
            if result:
                out.append(f"    первый элемент:")
                out.extend("    " + line for line in _describe_object(result[0], name="item", max_attrs=60))
        elif isinstance(result, dict):
            preview = ", ".join(f"{k!r}: {type(v).__name__}" for k, v in list(result.items())[:3])
            out.append(f"  {m}(): -> dict[{len(result)}] {{{preview}{'...' if len(result) > 3 else ''}}}")
            if result:
                first_key = next(iter(result))
                first_val = result[first_key]
                out.append(f"    первый ключ {first_key!r}, значение:")
                out.extend("    " + line for line in _describe_object(first_val, name="value", max_attrs=60))
        else:
            out.append(f"  {m}(): -> {type(result).__name__} = {repr(result)[:120]}")
    return out


async def main() -> int:
    setup_logging()
    settings = get_settings()

    out_lines: list[str] = []
    out_lines.append("=" * 70)
    out_lines.append(f"FunPay introspect — {datetime.now().isoformat(timespec='seconds')}")
    out_lines.append(f"FUNPAY_USER_ID: {settings.funpay_user_id}")
    out_lines.append("=" * 70)

    try:
        import FunPayAPI  # type: ignore
        out_lines.append(f"FunPayAPI module: {FunPayAPI.__file__}")
        version = getattr(FunPayAPI, "__version__", "unknown")
        out_lines.append(f"FunPayAPI version: {version}")
    except Exception as exc:
        out_lines.append(f"Не удалось импортировать FunPayAPI: {exc}")

    out_lines.append("")
    out_lines.append("# 1. Логин")

    async with FunPayClient() as fp:
        try:
            await fp.connect()
            out_lines.append(f"  account.id = {fp.account_id}")
            out_lines.append(f"  account.username = {fp.username}")
            out_lines.append(f"  account.balance (наш getter) = {fp.balance!r}")
        except Exception as exc:
            out_lines.append(f"  ERROR при логине: {exc}")
            _write_dump("\n".join(out_lines))
            return 2

        out_lines.append("")
        out_lines.append("# 2. Account object — все публичные атрибуты")
        out_lines.extend(_describe_object(fp.account, name="account"))

        out_lines.append("")
        out_lines.append("# 3. Пробуем методы для баланса")
        balance_methods = [
            "get_balance", "balance", "total_balance", "funds", "wallet",
            "get_funds", "money", "get_money",
        ]
        out_lines.extend(_try_methods(fp.account, balance_methods))

        out_lines.append("")
        out_lines.append("# 4. UserProfile (acc.get_user(acc.id))")
        try:
            profile = await asyncio.to_thread(fp.account.get_user, fp.account_id)
            out_lines.append(f"  тип: {type(profile).__name__}")
            out_lines.extend(_describe_object(profile, name="profile"))
        except Exception as exc:
            out_lines.append(f"  ERROR get_user: {exc}")
            profile = None

        out_lines.append("")
        out_lines.append("# 5. Пробуем все возможные пути получения списка лотов")
        lot_paths_on_account = [
            "get_my_lots", "get_lots", "lots", "get_all_lots",
            "get_my_subcategories", "get_subcategories",
        ]
        out_lines.append("## 5а. Account.*")
        out_lines.extend(_try_methods(fp.account, lot_paths_on_account))

        if profile is not None:
            out_lines.append("## 5б. UserProfile.*")
            lot_paths_on_profile = [
                "lots", "get_lots", "get_sorted_lots", "get_lot_pages",
                "subcategories", "get_subcategories",
            ]
            out_lines.extend(_try_methods(profile, lot_paths_on_profile))

        out_lines.append("")
        out_lines.append("# 6. Текущий get_my_lots() результат")
        try:
            lots = await fp.get_my_lots()
            out_lines.append(f"  длина: {len(lots)}")
            for i, lot in enumerate(lots[:3]):
                out_lines.append(f"  лот #{i}:")
                out_lines.extend("    " + l for l in _describe_object(lot, name=f"lot[{i}]", max_attrs=60))
        except Exception as exc:
            out_lines.append(f"  ERROR: {exc}")

    out_lines.append("")
    out_lines.append("=" * 70)
    out_lines.append("Готово. Файл можно прислать для адаптации клиента.")
    out_lines.append("=" * 70)

    path = _write_dump("\n".join(out_lines))
    logger.success(f"Дамп сохранён: {path}")
    logger.info(f"Размер: {path.stat().st_size} байт, строк: {len(out_lines)}")
    logger.info("Покажи мне этот файл (head -200) или скинь целиком.")
    return 0


def _write_dump(text: str) -> Path:
    settings = get_settings()
    out_dir = settings.data_path
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "funpay_introspect.txt"
    path.write_text(text, encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
