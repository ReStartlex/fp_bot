"""Async-сессия SQLAlchemy + создание таблиц при первом запуске."""
from __future__ import annotations

from loguru import logger
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.db.models import Base


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# Сколько миллисекунд SQLite ждёт освобождения замка БД до того, как
# поднять OperationalError("database is locked"). 30с с запасом покрывает
# любую штатную нагрузку: catalog_sync вставка ~50 строк ≈ 10мс, самая
# тяжёлая операция reconcile-loop ~1с. С 30с timeout «is locked» в проде
# означает реальный deadlock, а не просто параллельный writer.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        db_path = settings.data_path / "bridge.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        _engine = create_async_engine(
            url,
            echo=False,
            future=True,
            # Передаём в aiosqlite: один общий busy_timeout на уровне
            # драйвера. SQLAlchemy/aiosqlite по умолчанию использует
            # ~5с — мало для нашей конкурентной нагрузки (funpay_watcher
            # каждые 5с + sync_once 30с + catalog_sync 90с + cryptobot_poll
            # 30с одновременно бьются за writer-lock).
            connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000},
        )
        _apply_sqlite_pragmas(_engine)
    return _engine


def _apply_sqlite_pragmas(engine) -> None:
    """
    Включает WAL и подкручивает SQLite под наш профиль нагрузки.

    Что делаем при каждом новом подключении:

      PRAGMA journal_mode=WAL
        Главный фикс «database is locked». WAL разделяет writer/reader:
        один процесс пишет, остальные одновременно читают без блокировок.
        Файл .db-wal появляется рядом с bridge.db — это нормально.
        Без WAL DELETE-журнал держит эксклюзивный лок на всю БД во время
        каждого INSERT/UPDATE, и при 4+ воркерах локи копятся.

      PRAGMA synchronous=NORMAL
        В WAL это безопасно: fsync делается только при checkpoint'е,
        а не на каждую транзакцию. Даёт ~10x ускорение записи.
        FULL нужен только если боимся внезапной потери питания внутри
        транзакции — для нашего use-case (заказы переиграются reconciler'ом
        даже если упадём) NORMAL приемлем.

      PRAGMA busy_timeout
        Дублируем на уровне SQLite сам (не только драйвера). Если код
        запустит сырое .execute через подключение без timeout, оно
        всё равно подождёт.

      PRAGMA foreign_keys=ON
        SQLite по умолчанию ВЫКЛЮЧАЕТ FK-checks. Включаем явно —
        у нас есть FK-связи (shop_payments.order_id → shop_orders.id и др.).

      PRAGMA cache_size=-32000
        ~32 MiB в памяти под кэш страниц (по умолчанию ~2 MiB).
        Наша БД на проде <50 MiB — почти вся помещается в кэш.

      PRAGMA temp_store=MEMORY
        Временные таблицы/индексы из ORDER BY / GROUP BY — в RAM,
        а не в /tmp на диске.
    """
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA cache_size=-32000")
            cursor.execute("PRAGMA temp_store=MEMORY")
        finally:
            cursor.close()


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
        # Diff-based sync cache (см. Mapping модель в models.py)
        if "last_synced_price" not in columns:
            sync_conn.execute(text("ALTER TABLE mappings ADD COLUMN last_synced_price FLOAT"))
            logger.info("init_db: добавлена колонка mappings.last_synced_price")
        if "last_synced_stock" not in columns:
            sync_conn.execute(text("ALTER TABLE mappings ADD COLUMN last_synced_stock INTEGER"))
            logger.info("init_db: добавлена колонка mappings.last_synced_stock")
        if "last_synced_active" not in columns:
            sync_conn.execute(text("ALTER TABLE mappings ADD COLUMN last_synced_active BOOLEAN"))
            logger.info("init_db: добавлена колонка mappings.last_synced_active")
        if "last_synced_at" not in columns:
            sync_conn.execute(text("ALTER TABLE mappings ADD COLUMN last_synced_at DATETIME"))
            logger.info("init_db: добавлена колонка mappings.last_synced_at")
    if "orders" in tables:
        columns = {col["name"] for col in inspector.get_columns("orders")}
        for name in ("fx_rate_at_sale", "profit_rub", "profit_margin_percent"):
            if name not in columns:
                sync_conn.execute(text(f"ALTER TABLE orders ADD COLUMN {name} FLOAT"))
                logger.info(f"init_db: добавлена колонка orders.{name}")
        if "description" not in columns:
            sync_conn.execute(text("ALTER TABLE orders ADD COLUMN description TEXT"))
            logger.info("init_db: добавлена колонка orders.description")
        # Подтверждение успешного выполнения заказа (см. Order модель)
        if "confirmed_at" not in columns:
            sync_conn.execute(text("ALTER TABLE orders ADD COLUMN confirmed_at DATETIME"))
            logger.info("init_db: добавлена колонка orders.confirmed_at")
        if "confirmed_by" not in columns:
            sync_conn.execute(text("ALTER TABLE orders ADD COLUMN confirmed_by VARCHAR(16)"))
            logger.info("init_db: добавлена колонка orders.confirmed_by")
    if "chat_states" in tables:
        columns = {col["name"] for col in inspector.get_columns("chat_states")}
        for name in ("last_paid_order_at", "last_manual_message_at"):
            if name not in columns:
                sync_conn.execute(text(f"ALTER TABLE chat_states ADD COLUMN {name} DATETIME"))
                logger.info(f"init_db: добавлена колонка chat_states.{name}")
        if "manual_messages_count" not in columns:
            sync_conn.execute(
                text("ALTER TABLE chat_states ADD COLUMN manual_messages_count INTEGER DEFAULT 0")
            )
            logger.info("init_db: добавлена колонка chat_states.manual_messages_count")
    if "shop_catalog_cache" in tables:
        # Phase 1 Sprint 2.1: группировка категорий по «базовому имени»
        # (см. src/shop/taxonomy.py).
        columns = {col["name"] for col in inspector.get_columns("shop_catalog_cache")}
        added = False
        if "base_name" not in columns:
            sync_conn.execute(
                text("ALTER TABLE shop_catalog_cache ADD COLUMN base_name VARCHAR(255)")
            )
            logger.info("init_db: добавлена колонка shop_catalog_cache.base_name")
            added = True
        if "group_slug" not in columns:
            sync_conn.execute(
                text("ALTER TABLE shop_catalog_cache ADD COLUMN group_slug VARCHAR(16)")
            )
            logger.info("init_db: добавлена колонка shop_catalog_cache.group_slug")
            added = True
        # Backfill: заполняем base_name/group_slug для существующих записей,
        # чтобы /catalog сразу после деплоя показывал группы (а не ждал 90с
        # следующего catalog_sync'а). Делаем поштучно — записей ≤ нескольких
        # сотен, это секунда.
        if added:
            # Импорт здесь, а не наверху файла, чтобы не вводить циклическую
            # зависимость src.shop → src.db → src.shop.
            from src.shop.taxonomy import make_group_slug, parse_category_name

            rows = sync_conn.execute(text(
                "SELECT ns_service_id, category_name FROM shop_catalog_cache "
                "WHERE group_slug IS NULL"
            )).fetchall()
            for ns_service_id, category_name in rows:
                base_name, _ = parse_category_name(category_name or "")
                if not base_name:
                    base_name = f"Без названия #{ns_service_id}"
                slug = make_group_slug(base_name)
                sync_conn.execute(
                    text(
                        "UPDATE shop_catalog_cache "
                        "SET base_name = :bn, group_slug = :gs "
                        "WHERE ns_service_id = :sid"
                    ),
                    {"bn": base_name, "gs": slug, "sid": ns_service_id},
                )
            if rows:
                logger.info(
                    f"init_db: backfill base_name/group_slug для {len(rows)} услуг"
                )

    # Sprint 3: для shop_payments
    #   - order_id стал nullable (top-up не привязан к заказу);
    #     ALTER COLUMN ... DROP NOT NULL в SQLite не поддерживается,
    #     поэтому делаем это через rebuild table (SQLite-way) ТОЛЬКО если
    #     колонка реально NOT NULL — иначе no-op.
    #   - добавлена колонка `error` (текст ошибки если failed).
    if "shop_payments" in tables:
        cols_info = inspector.get_columns("shop_payments")
        col_names = {c["name"] for c in cols_info}
        if "error" not in col_names:
            sync_conn.execute(text("ALTER TABLE shop_payments ADD COLUMN error VARCHAR(255)"))
            logger.info("init_db: добавлена колонка shop_payments.error")
        # order_id: проверяем, NOT NULL ли он сейчас. Если да — rebuild.
        order_id_col = next((c for c in cols_info if c["name"] == "order_id"), None)
        if order_id_col is not None and order_id_col.get("nullable") is False:
            # SQLite rebuild: создаём новую таблицу с правильной схемой,
            # копируем данные, удаляем старую, переименовываем новую.
            # Все имена индексов/constraint-ов сохраняем.
            logger.info(
                "init_db: rebuild shop_payments для order_id → NULLABLE "
                "(SQLite не поддерживает ALTER COLUMN)"
            )
            sync_conn.execute(text("""
                CREATE TABLE shop_payments_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    provider VARCHAR(16) NOT NULL,
                    provider_invoice_id VARCHAR(128) NOT NULL,
                    amount_kopecks INTEGER NOT NULL,
                    currency VARCHAR(8) NOT NULL DEFAULT 'RUB',
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    raw_payload_json TEXT,
                    error VARCHAR(255),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    paid_at DATETIME,
                    CONSTRAINT uq_shop_payments_provider_invoice
                        UNIQUE (provider, provider_invoice_id)
                )
            """))
            sync_conn.execute(text("""
                INSERT INTO shop_payments_new
                    (id, order_id, provider, provider_invoice_id,
                     amount_kopecks, currency, status, raw_payload_json,
                     error, created_at, paid_at)
                SELECT
                    id, order_id, provider, provider_invoice_id,
                    amount_kopecks, currency, status, raw_payload_json,
                    NULL, created_at, paid_at
                FROM shop_payments
            """))
            sync_conn.execute(text("DROP TABLE shop_payments"))
            sync_conn.execute(text(
                "ALTER TABLE shop_payments_new RENAME TO shop_payments"
            ))
            logger.info("init_db: shop_payments rebuild завершён")


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
