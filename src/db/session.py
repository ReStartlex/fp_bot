"""Async-сессия SQLAlchemy + создание таблиц при первом запуске."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.db.models import Base


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        db_path = settings.data_path / "bridge.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        _engine = create_async_engine(url, echo=False, future=True)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def init_db() -> None:
    """
    Создаёт таблицы, если их ещё нет. Идемпотентно: `create_all`
    не трогает существующие данные.

    Логируем, какие таблицы реально существуют после init — это важно
    для диагностики на VPS (после добавления модели легко забыть, что
    у пользователя уже есть старая БД без новой таблицы — create_all
    добавит её, но мы хотим явное подтверждение в логе).
    """
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sqlite_schema)
        existing = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_table_names()
        )
    expected = set(Base.metadata.tables.keys())
    missing = expected - set(existing)
    if missing:
        logger.warning(f"init_db: после create_all отсутствуют таблицы: {missing}")
    else:
        logger.info(
            f"init_db: все таблицы на месте ({len(existing)}): "
            f"{sorted(existing)}"
        )

    from src.db.repo import ensure_default_lot_groups

    async with session_factory()() as session:
        await ensure_default_lot_groups(session)
        await session.commit()


def _migrate_sqlite_schema(sync_conn) -> None:
    """Мини-миграции для существующей SQLite-БД без Alembic."""
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    if "mappings" in tables:
        columns = {col["name"] for col in inspector.get_columns("mappings")}
        if "group_id" not in columns:
            sync_conn.execute(text("ALTER TABLE mappings ADD COLUMN group_id INTEGER"))
            logger.info("init_db: добавлена колонка mappings.group_id")


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
