"""
Тесты SQLite-PRAGMA конфигурации.

Проверяем, что после `init_db()` каждое новое подключение получает
WAL + busy_timeout + foreign_keys ON. Это критично для исключения
«database is locked» при конкурентной записи нескольких воркеров.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture()
async def real_engine(tmp_path, monkeypatch):
    """
    Реальный engine с файловой БД (in-memory WAL не имеет смысла —
    проверка делается на реальном файле).
    """
    import src.db.session as session_mod
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    saved_engine = session_mod._engine
    saved_factory = session_mod._session_factory
    session_mod._engine = None
    session_mod._session_factory = None

    # Подменяем data_path → tmp_path через monkeypatch settings cache
    from src.config import get_settings
    s = get_settings()
    # data_path — property, поэтому подменяем data_dir base
    monkeypatch.setattr(s, "_data_dir_override", tmp_path, raising=False)
    # И самое надёжное — переопределяем _get_engine:
    db_path = tmp_path / "bridge.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(
        url, echo=False, future=True,
        connect_args={"timeout": 30},
    )
    session_mod._apply_sqlite_pragmas(engine)
    session_mod._engine = engine
    session_mod._session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession,
    )

    yield engine

    await engine.dispose()
    session_mod._engine = saved_engine
    session_mod._session_factory = saved_factory


async def test_wal_mode_enabled(real_engine):
    async with real_engine.connect() as conn:
        res = await conn.execute(text("PRAGMA journal_mode"))
        mode = res.scalar()
    assert str(mode).lower() == "wal", f"expected WAL, got {mode!r}"


async def test_busy_timeout_set(real_engine):
    async with real_engine.connect() as conn:
        res = await conn.execute(text("PRAGMA busy_timeout"))
        timeout = res.scalar()
    # Должен быть ≥ 30000ms
    assert int(timeout) >= 30000, f"busy_timeout too low: {timeout}"


async def test_synchronous_is_normal(real_engine):
    async with real_engine.connect() as conn:
        res = await conn.execute(text("PRAGMA synchronous"))
        sync_mode = res.scalar()
    # 1 = NORMAL, 2 = FULL, 0 = OFF
    assert int(sync_mode) == 1, f"expected NORMAL(1), got {sync_mode}"


async def test_foreign_keys_on(real_engine):
    async with real_engine.connect() as conn:
        res = await conn.execute(text("PRAGMA foreign_keys"))
        fk = res.scalar()
    assert int(fk) == 1, f"foreign_keys must be ON, got {fk}"


async def test_concurrent_writes_dont_lock(real_engine):
    """
    Smoke-тест: 5 параллельных INSERT'ов из разных сессий не должны
    падать с 'database is locked'. До WAL — без вариантов падали бы.
    """
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(real_engine, expire_on_commit=False)

    # Создаём минимальную тестовую таблицу
    async with real_engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS load_test "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, v INTEGER)"
        ))

    async def writer(n: int):
        async with factory() as s:
            for i in range(20):
                await s.execute(
                    text("INSERT INTO load_test (v) VALUES (:v)"),
                    {"v": n * 100 + i},
                )
            await s.commit()

    await asyncio.gather(*(writer(i) for i in range(5)))

    async with factory() as s:
        res = await s.execute(text("SELECT COUNT(*) FROM load_test"))
        count = res.scalar()
    assert count == 100  # 5 writers * 20 inserts
