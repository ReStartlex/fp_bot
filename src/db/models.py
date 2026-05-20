"""SQLAlchemy-модели локальной БД."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class LotGroup(Base):
    """Группа лотов: промежуточный уровень правил между global и mapping."""
    __tablename__ = "lot_groups"
    __table_args__ = (UniqueConstraint("slug", name="uq_lot_groups_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    match_keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    markup_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stock_cap: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class Mapping(Base):
    """Связка FunPay лот -> NS service_id + правила цены/стока."""
    __tablename__ = "mappings"
    __table_args__ = (UniqueConstraint("funpay_lot_id", name="uq_mappings_funpay_lot_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    funpay_lot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ns_service_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # Опциональные override глобальных настроек:
    markup_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stock_cap: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    group_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Шаблон fields для NS create_order. JSON-строка.
    # Например: '{"quantity": "@QUANTITY"}'. @QUANTITY = количество из FunPay-заказа.
    ns_fields_template: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class FxRate(Base):
    """Кэш курсов валют."""
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair: Mapped[str] = mapped_column(String(16), nullable=False)  # 'USD/RUB'
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class SyncRun(Base):
    """История запусков синхронизатора."""
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    lots_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lots_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lots_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ChatState(Base):
    """Состояние чата с покупателем: когда здоровались, когда просили помощь."""
    __tablename__ = "chat_states"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    greeted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_help_request_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    help_requests_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RuntimeSetting(Base):
    """
    Runtime-override для глобальных параметров. Если ключ есть в этой
    таблице — берётся он, иначе fallback к Settings (из .env).
    Хранится как строка, парсится на стороне читателя.

    Ключи в использовании:
        global_markup_percent
        usd_rub_premium_percent
        funpay_stock_cap
    """
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class FunpayChatCursor(Base):
    """
    Курсор последнего обработанного сообщения в FunPay-чате.

    Зачем: watcher должен переживать рестарты. Без БД-курсора каждый
    рестарт стартует с in-memory baseline'а, который не знает, какие
    сообщения уже обработаны. Из-за этого после рестарта бот мог либо
    проиграть старые `!помощь` ещё раз, либо пропустить новое сообщение.

    Контракт:
    - last_message_id — id ПОСЛЕДНЕГО успешно «увиденного» (диспатченного
      в handler) сообщения. Все сообщения с id > last_message_id —
      ещё не обработаны.
    - При первом старте для нового чата запись создаётся с
      last_message_id = id текущего самого свежего сообщения, и от него
      бот двигается дальше.
    """
    __tablename__ = "funpay_chat_cursors"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_message_text_hash: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnownLot(Base):
    """
    FunPay-лоты, которые мы уже видели у нашего аккаунта. Сравнивая
    свежий список из FunPay со списком в этой таблице, ловим
    появившиеся «новые» лоты и шлём про них алерт.
    """
    __tablename__ = "known_lots"

    funpay_lot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # Помечается True один раз, после первого пуша в Telegram —
    # чтобы не шуметь повторно.
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Order(Base):
    """Заказ с FunPay -> NS pipeline."""
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("funpay_order_id", name="uq_orders_funpay"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    funpay_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    funpay_lot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ns_service_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ns_custom_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    buyer_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    buyer_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    funpay_price_rub: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ns_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    # Возможные статусы: received, ns_created, ns_paid, delivered, failed, refunded
    pins_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
