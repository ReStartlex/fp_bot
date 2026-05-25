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

    # === Diff-based sync cache ===
    # Зачем: sync_stock каждые 30 сек делает 47 GET к FunPay (`get_lot_fields`)
    # чтобы проверить, не разошлись ли наши и FunPay-цены. На практике
    # 46 из 47 лотов НЕ меняются между циклами (в проде видно: updated=1
    # стабильно). Эти GET зря тратят rate-limit и провоцируют 429.
    #
    # Решение: после успешного save_lot запоминаем `(price, stock, active)`.
    # В следующем цикле, если target из NS совпадает с last_synced и
    # last_synced свежий (TTL), пропускаем FunPay-запрос целиком.
    # Раз в TTL делаем «полный» цикл — на случай если кто-то менял
    # цены на FunPay вручную через их UI, чтобы наш cache не разъехался.
    #
    # NULL = «ещё не синхронизировался» (первый прогон после миграции
    # или после ручного сброса). Fast-path в этом случае не применяется.
    last_synced_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_synced_stock: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_synced_active: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

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
    last_paid_order_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_manual_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    manual_messages_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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
    fx_rate_at_sale: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_rub: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_margin_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    # Возможные статусы:
    #   received → ns_created → ns_paid → pins_ready → delivering → delivered
    #   На любом шаге: failed | refunded | manual_hold
    #
    # `delivering` — промежуточный статус «send_message в FunPay в процессе».
    # Аудит #3: если crash между success send_message и commit'ом delivered,
    # статус останется delivering, и reconciler НЕ повторит отправку
    # автоматически (риск дубля), а переведёт в manual_hold для оператора.
    pins_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Описание лота как пришло из FunPay-события. Нужно reconciler'у,
    # чтобы после рестарта он мог повторно сматчить маппинг по
    # описанию (если funpay_lot_id=0 — старый формат событий без lot_id).
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # === Подтверждение успешного выполнения заказа ===
    # FunPay шлёт системное сообщение когда покупатель ИЛИ администратор
    # support'а нажимает «подтвердить выполнение» (после 24ч ожидания
    # покупатель может попросить саппорт сделать это вручную; ~50% наших
    # клиентов сами никогда не подтверждают).
    #
    # confirmed_at = когда пришло системное сообщение от FunPay (UTC)
    # confirmed_by = кто подтвердил: "buyer" (сам покупатель) или
    #                "admin" (саппорт FunPay по нашему запросу)
    #                NULL = ещё не подтверждён.
    #
    # Используется командой /pending_confirm в Telegram-боте: список
    # заказов status=delivered, прошло >24ч, confirmed_at=NULL —
    # это именно те заказы, которые нужно отправить в саппорт.
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


# ════════════════════════════════════════════════════════════════════════
#                    Phase 1: TG-shop модели
# ════════════════════════════════════════════════════════════════════════
# Все таблицы префикс shop_, чтобы аналитика и backup отделяли их от
# FunPay-pipeline. Живут в той же bridge.db (один engine, один backup).
# Деньги — в копейках Integer, чтобы не накапливать float-погрешность
# на 1%-кэшбэке и многократных списаниях/начислениях.


class ShopUser(Base):
    """Покупатель в Telegram-магазине."""
    __tablename__ = "shop_users"
    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_shop_users_tg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    language_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    # Внутренний баланс (кэшбэк, рефуанды). Храним в копейках, чтобы
    # 1%-начисления не накапливали float-погрешность.
    balance_kopecks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Кто пригласил (FK на shop_users.id). Null = пришёл сам.
    # Дублирует ShopReferral.referrer_user_id для быстрых выборок без JOIN,
    # но source-of-truth — таблица ShopReferral (там UNIQUE constraint).
    referred_by_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Если оператор забанил клиента (например, попытка чарджбэка).
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ShopReferral(Base):
    """
    Связь реферал → пригласивший. Один реферал = один inviter навсегда.
    UNIQUE по referred_user_id защищает от перепривязки и двойных начислений
    при попытке повторно «зарегистрироваться по чужой ссылке».
    """
    __tablename__ = "shop_referrals"
    __table_args__ = (UniqueConstraint("referred_user_id", name="uq_shop_referrals_referred"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    referred_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ShopOrder(Base):
    """
    Заказ из shop-бота, который идёт в NS.

    Жизненный цикл:
        draft → awaiting_payment → paid → ns_created → ns_paid
              → delivering → delivered
        На любом шаге: payment_failed | failed | manual_hold | refunded

    Idempotency: ns_custom_id = f"shop-{shop_order.id}", уникален навсегда.
    Перед NS.create_order() processor вызывает NS.order_info(custom_id):
    если заказ уже есть в NS, переиспользуем его (защита от двойной NS-покупки).
    """
    __tablename__ = "shop_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # Snapshot карточки NS на момент покупки (на случай если NS уберёт услугу).
    ns_service_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ns_service_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Поля для NS create_order — JSON-список dict'ов.
    fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Что мы реально взяли с покупателя (копейки RUB).
    total_rub_kopecks: Mapped[int] = mapped_column(Integer, nullable=False)
    # Сколько списано с внутреннего баланса покупателя.
    balance_used_kopecks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Сколько ушло во внешнюю оплату (CryptoBot/Stars).
    # external_paid = total - balance_used; держим явно для устойчивости
    # к расхождениям с провайдером (если фактически списали меньше — увидим).
    external_paid_kopecks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Snapshot для post-mortem аналитики.
    ns_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fx_rate_at_sale: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    markup_percent_at_sale: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # NS pipeline (заполняется processor'ом).
    ns_custom_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ns_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pins_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Метод внешней оплаты (cryptobot|stars|balance_only).
    payment_method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ShopPayment(Base):
    """
    Внешний платёж за shop-заказ (CryptoBot / Telegram Stars).
    UNIQUE(provider, provider_invoice_id) защищает от replay-атаки webhook'а:
    даже если CryptoBot пришлёт нам один и тот же invoice дважды, INSERT
    упадёт и мы не зачислим деньги повторно.
    """
    __tablename__ = "shop_payments"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_invoice_id",
            name="uq_shop_payments_provider_invoice",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # cryptobot | stars
    provider_invoice_id: Mapped[str] = mapped_column(String(128), nullable=False)

    amount_kopecks: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")

    # pending | paid | expired | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    raw_payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ShopBalanceLedger(Base):
    """
    Журнал движений внутреннего баланса (append-only, double-entry-аудит).

    Контракт: сумма всех change_kopecks для user_id ≡ ShopUser.balance_kopecks.
    Любой код, который меняет shop_users.balance_kopecks, ОБЯЗАН добавить
    запись в ledger в той же транзакции. Это даёт нам аудит-trail для
    спорных кейсов («куда делись мои 50₽?») и инвариант для тестов.
    """
    __tablename__ = "shop_balance_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # +N (начисление) или -N (списание). Никогда не 0.
    change_kopecks: Mapped[int] = mapped_column(Integer, nullable=False)

    # referral_cashback | order_payment | refund | manual_admin
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    related_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ShopCatalogCache(Base):
    """
    Snapshot каталога NS, обновляется фоновым воркером раз в
    shop_catalog_refresh_seconds. UI бота читает только отсюда — мгновенный
    ответ. Если воркер упал, кеш «стареет», но не пустеет, и shop продолжает
    продавать по последним известным ценам (с алертом владельцу в Telegram).
    """
    __tablename__ = "shop_catalog_cache"

    ns_service_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    category_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    service_name: Mapped[str] = mapped_column(String(255), nullable=False)

    ns_price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    rub_price_kopecks: Mapped[int] = mapped_column(Integer, nullable=False)
    in_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Схема полей для NS create_order — JSON-список FieldType dict'ов.
    # Нужна на checkout: показываем форму "введите email" / "введите username".
    fields_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Управляется оператором: если NS-услуга проблемная — выключаем тут,
    # покупатели не увидят её в каталоге.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
