"""
Тесты миграции shop_catalog_cache: добавление base_name/group_slug
с backfill существующих записей.

Сценарий продакшна:
1. Старая схема без этих колонок, есть N записей каталога.
2. Деплой → init_db делает ALTER TABLE.
3. Сразу же backfill: для каждой записи парсим category_name и считаем slug.
4. /catalog после рестарта показывает группы немедленно (без ожидания
   следующего catalog_sync'а ≤90с).

Тестируем _migrate_sqlite_schema напрямую с sync engine — это та же
функция, что вызывается из init_db через run_sync.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.db.session import _migrate_sqlite_schema
from src.shop.taxonomy import make_group_slug


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


def _build_legacy_schema(db_path: str) -> None:
    """
    Создаём БД в схеме «до Sprint 2.1»: shop_catalog_cache без
    base_name и group_slug.
    """
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE shop_catalog_cache (
                ns_service_id INTEGER PRIMARY KEY,
                category_id INTEGER,
                category_name VARCHAR(128),
                service_name VARCHAR(255) NOT NULL,
                ns_price_usd FLOAT NOT NULL,
                rub_price_kopecks INTEGER NOT NULL,
                in_stock INTEGER NOT NULL DEFAULT 0,
                fields_json TEXT,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                fetched_at DATETIME
            )
        """))
        for sid, cid, cname, sname, price_k in [
            (1, 10, "Apple Gift Card | US", "Apple US $5", 40000),
            (2, 11, "Apple Gift Card | EU", "Apple EU €5", 44000),
            (3, 20, "Steam", "Steam $5", 40000),
        ]:
            conn.execute(text(
                "INSERT INTO shop_catalog_cache "
                "(ns_service_id, category_id, category_name, service_name, "
                " ns_price_usd, rub_price_kopecks, in_stock, fetched_at) "
                "VALUES (:sid, :cid, :cname, :sname, 5.0, :pk, 10, datetime('now'))"
            ), {"sid": sid, "cid": cid, "cname": cname, "sname": sname, "pk": price_k})
    engine.dispose()


def test_migration_adds_columns_and_backfills(db_path):
    """ALTER + backfill для всех существующих записей."""
    _build_legacy_schema(db_path)

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        _migrate_sqlite_schema(conn)
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT ns_service_id, category_name, base_name, group_slug "
            "FROM shop_catalog_cache ORDER BY ns_service_id"
        )).fetchall()
    engine.dispose()

    assert len(rows) == 3
    by_id = {r[0]: r for r in rows}
    assert by_id[1][2] == "Apple Gift Card"
    assert by_id[1][3] == make_group_slug("Apple Gift Card")
    # Apple EU: тот же base_name → тот же slug
    assert by_id[2][2] == "Apple Gift Card"
    assert by_id[2][3] == make_group_slug("Apple Gift Card")
    assert by_id[1][3] == by_id[2][3]
    # Steam: без variant → base_name == "Steam"
    assert by_id[3][2] == "Steam"
    assert by_id[3][3] == make_group_slug("Steam")


def test_migration_is_idempotent(db_path):
    """Повторный вызов миграции над уже-мигрированной БД не падает."""
    _build_legacy_schema(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        _migrate_sqlite_schema(conn)
    # Второй прогон — должен пройти без ошибок (alter пропускается,
    # backfill ничего не находит т.к. group_slug уже заполнен).
    with engine.begin() as conn:
        _migrate_sqlite_schema(conn)
    # Данные не повредились
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT COUNT(*) FROM shop_catalog_cache WHERE group_slug IS NOT NULL"
        )).scalar()
    engine.dispose()
    assert rows == 3


def test_migration_handles_empty_table(db_path):
    """Если таблица существует, но пустая — миграция отрабатывает без рядов."""
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE shop_catalog_cache (
                ns_service_id INTEGER PRIMARY KEY,
                category_name VARCHAR(128),
                service_name VARCHAR(255) NOT NULL,
                ns_price_usd FLOAT NOT NULL,
                rub_price_kopecks INTEGER NOT NULL,
                in_stock INTEGER NOT NULL DEFAULT 0,
                enabled BOOLEAN NOT NULL DEFAULT 1
            )
        """))
        _migrate_sqlite_schema(conn)
    engine.dispose()


def test_migration_handles_null_category_name(db_path):
    """Запись с category_name=NULL получает placeholder base_name."""
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE shop_catalog_cache (
                ns_service_id INTEGER PRIMARY KEY,
                category_name VARCHAR(128),
                service_name VARCHAR(255) NOT NULL,
                ns_price_usd FLOAT NOT NULL,
                rub_price_kopecks INTEGER NOT NULL,
                in_stock INTEGER NOT NULL DEFAULT 0,
                enabled BOOLEAN NOT NULL DEFAULT 1
            )
        """))
        conn.execute(text(
            "INSERT INTO shop_catalog_cache "
            "(ns_service_id, category_name, service_name, ns_price_usd, "
            " rub_price_kopecks, in_stock) VALUES (99, NULL, 'X', 1, 100, 1)"
        ))
        _migrate_sqlite_schema(conn)
        row = conn.execute(text(
            "SELECT base_name, group_slug FROM shop_catalog_cache WHERE ns_service_id=99"
        )).fetchone()
    engine.dispose()
    # Placeholder: "Без названия #99"
    assert row[0] == "Без названия #99"
    assert row[1] == make_group_slug("Без названия #99")

