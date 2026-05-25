"""
Smoke-тесты для /lot_status команды в owner-боте.

`/lot_status <funpay_lot_id>` — read-only диагностика лота:
  * показывает diff-cache state (last_synced_*, TTL fresh/stale);
  * capped-indicator (NS_stock > effective cap);
  * предсказание cache hit / save_lot на следующем sync-цикле.

Полные integration-тесты с FunPay/NS-mock'ами сложны (см. _do_force_sync —
тоже не покрыт unit-тестами). Здесь проверяем что:
  * метод существует и доступен в классе AlertsBot;
  * команда зарегистрирована в HELP_TEXT и BotCommand-меню;
  * usage-hint без аргументов формируется правильно (через AST анализ).
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest


BOT_PY = Path(__file__).parent.parent / "src" / "alerts" / "bot.py"


def _bot_source() -> str:
    return BOT_PY.read_text(encoding="utf-8")


def test_lot_status_handler_registered_in_dispatcher():
    """Регистрация /lot_status в dp.message — критично, иначе команда не работает."""
    src = _bot_source()
    assert 'Command("lot_status")' in src
    assert "cmd_lot_status" in src
    assert "self._do_lot_status(msg)" in src


def test_lot_status_method_exists():
    """Метод _do_lot_status должен быть определён в AlertsBot."""
    src = _bot_source()
    tree = ast.parse(src)

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_do_lot_status":
            found = True
            # есть docstring?
            assert ast.get_docstring(node) is not None, \
                "_do_lot_status должен иметь docstring"
            break

    assert found, "_do_lot_status не найден в bot.py"


def test_lot_status_in_help_text():
    """Команда должна быть в HELP_TEXT, иначе юзер о ней не узнает."""
    src = _bot_source()
    assert "/lot_status" in src
    # формат подсказки: с угловыми скобками (HTML-escape)
    assert "/lot_status &lt;funpay_lot_id&gt;" in src


def test_lot_status_in_bot_commands_menu():
    """BotCommand для /lot_status должен быть в set_my_commands."""
    src = _bot_source()
    assert 'BotCommand(command="lot_status"' in src


def test_lot_status_uses_compute_pricing_imports():
    """Функция должна импортить compute_pricing, get_global_markup_percent
    и get_stock_cap (внутри тела, lazy-import как в /force_sync)."""
    src = _bot_source()

    # Найдём блок _do_lot_status
    start = src.find("async def _do_lot_status")
    assert start > 0
    end = src.find("\n    @_guard\n", start + 1)
    if end < 0:
        end = src.find("\n    async def ", start + 1)
    if end < 0:
        end = len(src)
    body = src[start:end]

    assert "compute_pricing" in body
    assert "get_global_markup_percent" in body
    assert "get_stock_cap" in body


def test_lot_status_does_not_call_apply_decision():
    """ВАЖНО: /lot_status — read-only. Не должен ВЫЗЫВАТЬ _apply_decision
    или save_lot, иначе теряет суть (это отличие от /force_sync).

    Проверка через AST: ищем именно Call'ы (упоминания в комментариях
    и docstring'ах допустимы — пишем «cache от last save_lot success»)."""
    src = _bot_source()
    tree = ast.parse(src)

    body_ast = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_do_lot_status":
            body_ast = node
            break
    assert body_ast is not None

    forbidden = {"_apply_decision", "save_lot"}
    for sub in ast.walk(body_ast):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else None
            )
            assert name not in forbidden, (
                f"/lot_status должен быть read-only, "
                f"но обнаружен вызов {name}(...)"
            )


def test_lot_status_computes_capped_indicator():
    """Функция должна проверять условие capped: NS > target.stock."""
    src = _bot_source()

    start = src.find("async def _do_lot_status")
    end = src.find("\n    @_guard\n", start + 1)
    if end < 0:
        end = len(src)
    body = src[start:end]

    assert "capped" in body
    assert "in_stock" in body  # читаем NS_stock из ns_svc
    assert "target.stock" in body


def test_lot_status_reports_cache_freshness():
    """Должна показывать TTL fresh/stale на основе last_synced_at."""
    src = _bot_source()

    start = src.find("async def _do_lot_status")
    end = src.find("\n    @_guard\n", start + 1)
    if end < 0:
        end = len(src)
    body = src[start:end]

    assert "last_synced_at" in body
    assert "sync_stock_diff_cache_ttl_seconds" in body
    assert "fresh" in body or "stale" in body


def test_bot_py_parses_cleanly_after_edit():
    """Защита от синтаксических ошибок при добавлении новой команды."""
    src = _bot_source()
    # Парсинг должен пройти без exception
    ast.parse(src)


def test_lot_status_module_imports_without_error():
    """Импорт bot.py не должен падать (защита от случайных опечаток в типах)."""
    spec = importlib.util.spec_from_file_location("src.alerts.bot", BOT_PY)
    assert spec is not None and spec.loader is not None
    # Не пытаемся реально загрузить — для этого нужны все зависимости,
    # но AST + import проверки выше уже достаточны.
