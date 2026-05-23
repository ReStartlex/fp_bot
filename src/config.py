"""Загрузка конфигурации из .env с валидацией через pydantic."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Currency(str, Enum):
    RUB = "RUB"
    USD = "USD"
    EUR = "EUR"


class RateMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class ProxyType(str, Enum):
    SOCKS5 = "socks5"
    HTTP = "http"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    enable_real_actions: bool = False
    sync_interval_seconds: int = Field(default=30, ge=10)
    # Интервал поиска новых FunPay-лотов и пуша алертов про них.
    # Лоты обнаруживаются и пушатся только один раз каждому ID.
    new_lots_check_interval_seconds: int = Field(default=180, ge=30)
    new_lots_notify_enabled: bool = True
    new_lots_suggest_enabled: bool = True
    new_lots_suggest_max: int = Field(default=3, ge=0, le=5)
    new_lots_suggest_min_score: int = Field(default=20, ge=0)
    timezone: str = "Europe/Moscow"
    ns_use_playground: bool = False

    ns_base_url: str = "https://api.ns.gifts"
    ns_user_id: int
    ns_login: str
    ns_password: SecretStr
    ns_api_secret: SecretStr
    ns_totp_secret: SecretStr | None = None
    ns_low_balance_threshold: float = 20.0

    funpay_golden_key: SecretStr
    funpay_phpsessid: SecretStr | None = None
    funpay_user_id: int
    funpay_chat_language: Literal["ru", "en"] = "ru"

    markup_percent: float = Field(default=5.0, ge=0)
    funpay_currency: Currency = Currency.RUB
    usd_rub_rate_mode: RateMode = RateMode.AUTO
    usd_rub_rate: float = Field(default=90.0, gt=0)
    # Премия к биржевому курсу: разница между ЦБ-курсом и реальной ценой
    # покупки USD на бирже (Bybit и т.п.). 3.0 = +3% поверх курса ЦБ.
    # Применяется только в режиме AUTO; в MANUAL premium не используется,
    # потому что юзер сам задал нужный курс.
    usd_rub_premium_percent: float = Field(default=3.0, ge=0, le=50)
    price_update_threshold_percent: float = Field(default=2.0, ge=0)
    funpay_stock_cap: int = Field(default=100, ge=1)
    sync_min_margin_percent: float = Field(default=1.0)
    sync_max_price_change_percent: float = Field(default=50.0, ge=0)
    sync_reserve_pending_orders: bool = True

    # === Diff-based sync_stock cache ===
    # sync_stock каждый цикл проверяет 47 лотов и делает 47 GET к
    # FunPay (`get_lot_fields`). Но 46 из 47 не меняются между циклами
    # (в проде стабильно `updated=1`). Эти GET зря тратят rate-limit.
    #
    # Решение: запоминаем последний успешный sync (`mappings.last_synced_*`)
    # и пропускаем FunPay-запросы, если NS-target совпадает с cache.
    # Раз в TTL делаем «полный» цикл — на случай если кто-то правил цены
    # на FunPay вручную через их UI (наш cache мог разъехаться).
    #
    # Эффект: ожидаемо снижение GET'ов в ~10-15 раз → 429 пропадает
    # как класс. Управляется отдельным флагом, чтобы можно было
    # быстро откатить через .env (без передеплоя), если что.
    sync_stock_diff_cache_enabled: bool = True
    # 300 сек = 5 минут. Каждые 5 минут принудительно делаем GET,
    # чтобы cache не разъехался с FunPay.
    sync_stock_diff_cache_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    # Комиссия FunPay для оценки цены клиента: чисто справочно для /calc.
    # Не влияет на то, какую цену мы записываем (мы пишем цену продавца, FunPay
    # сам добавит комиссию). Реальная комиссия зависит от категории.
    funpay_commission_percent: float = Field(default=12.5, ge=0, le=50)
    # Реальная потеря при выводе средств с FunPay. Учитывается в прибыли,
    # но не влияет на цену лота: цену контролируют markup и FX premium.
    funpay_withdrawal_fee_percent: float = Field(default=3.0, ge=0, le=50)

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    telegram_enabled: bool = True
    telegram_use_proxy: bool = False
    telegram_proxy_type: ProxyType = ProxyType.SOCKS5
    telegram_proxy_host: str | None = None
    telegram_proxy_port: int | None = None
    telegram_proxy_username: SecretStr | None = None
    telegram_proxy_password: SecretStr | None = None

    web_api_enabled: bool = False
    web_api_host: str = "127.0.0.1"
    web_api_port: int = Field(default=8080, ge=1, le=65535)
    web_api_token: SecretStr | None = None

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "logs"

    funpay_update_rate_limit_per_second: float = Field(default=1.0, gt=0)

    # Поведение при HTTP 429 от FunPay (rate-limit). Применяется ко всем
    # обращениям FunPayAdminClient: GET /lots/offerEdit, GET /chat/,
    # POST /lots/offerSave, POST /runner/.
    #
    # Логика:
    #   sleep_before_retry = Retry-After (если прислан и парсится как число)
    #                        иначе  min(base * 2^attempt, max)
    #
    # Зачем: в горячий момент (массовый sync_stock или ответ FunPay при
    # пике трафика) FunPay начинает выкидывать 429. Без ретраев это
    # выливалось в `FunPay save_lot(...) NOT OK: http=429` и заставляло
    # стоковый sync пропускать лот целиком (полный slot терялся).
    # Терпеливый exponential backoff с уважением Retry-After разруливает
    # это без потерь.
    funpay_429_max_retries: int = Field(default=4, ge=0, le=10)
    funpay_429_base_backoff_seconds: float = Field(default=1.0, gt=0, le=30.0)
    funpay_429_max_backoff_seconds: float = Field(default=30.0, gt=0, le=120.0)

    # Поведение GET-запросов при 5xx Bad Gateway / Service Unavailable /
    # Gateway Timeout от FunPay (видимая проблема: лог 23.05.2026 показал
    # массовые `get_lot_fields ... 502 Server Error: Bad Gateway`,
    # из-за чего sync_stock пропускал лоты целиком).
    #
    # Применяется только к GET (идемпотентные). POST 5xx (save_lot,
    # send_message) НЕ ретраится автоматически: для send_message это
    # риск двойной отправки сообщения. base/max backoff общие с
    # funpay_429_*. Сетевые ошибки (ConnectionError и т.п.) тоже
    # используют этот счётчик, потому что семантически они равны
    # "сервер недоступен".
    #
    # 5xx у FunPay обычно transient и рассасывается за 1-5 секунд,
    # поэтому 2 retries (всего 3 попытки) достаточно: 1s, 2s — суммарно
    # ~3 секунды задержки. Если FunPay лежит дольше — лучше пропустить
    # лот и попробовать в следующем sync-цикле через 30 секунд.
    funpay_5xx_max_retries: int = Field(default=2, ge=0, le=10)

    # === Глобальный rate-limit на исходящие FunPay-запросы ===
    # Предотвращает 429 ДО их возникновения, вместо того чтобы только
    # реагировать после. Применяется к GET и POST одновременно
    # (общий счётчик).
    #
    # Видимая в проде проблема (23.05.2026, через несколько секунд
    # после старта): на одном offerEdit URL мы получали 429 → 429 →
    # 200, на 4 разных URL подряд. Это значит FunPay активно нас
    # ограничивает при текущем стиле массовых запросов из
    # sync_stock + chat-watcher.
    #
    # max_concurrent: сколько HTTP-запросов могут идти одновременно.
    #   4 — консервативно (asyncio thread pool обычно 6-10, оставляем
    #   запас на другие thread-вызовы: SQLite, обработчики и т.п.).
    # min_interval_seconds: минимальная пауза между ЛЮБЫМИ двумя
    #   запросами. 0.1 = max 10 RPS даже при concurrent=1.
    funpay_rate_max_concurrent: int = Field(default=4, ge=1, le=32)
    funpay_rate_min_interval_seconds: float = Field(default=0.1, ge=0.0, le=10.0)
    ns_retry_attempts: int = Field(default=3, ge=1)
    ns_retry_delay_seconds: float = Field(default=5.0, gt=0)
    ns_order_poll_interval_seconds: float = Field(default=5.0, gt=0)
    ns_order_timeout_seconds: float = Field(default=600.0, gt=0)
    order_reconcile_enabled: bool = True
    order_reconcile_interval_seconds: int = Field(default=120, ge=30)
    order_reconcile_stale_after_seconds: int = Field(default=60, ge=0)
    order_reconcile_max_per_run: int = Field(default=10, ge=1, le=100)

    # Жёсткий лимит на ПОЛНЫЙ цикл received→delivered. По истечении бот:
    #   1) переводит заказ в manual_hold (а не failed), чтобы заказ
    #      остался виден в /problems и доступен для ручного retry;
    #   2) аварийно выключает FunPay-лот (чтобы новые покупки не ушли
    #      в ту же ловушку);
    #   3) шлёт в Telegram алерт с кнопками "Retry/Выдано вручную/Детали".
    # Главная задача — не оставлять покупателя без выдачи бесконечно
    # ("после получения денег покупатель должен получить товар или
    # увидеть оператора в адекватные сроки"), и одновременно не дать
    # боту "догнать" оператора, если тот уже выдал товар вручную.
    # Диапазон 5..30 мин: меньше — слишком агрессивно для медленных
    # NS-выдач, больше — покупатель долго ждёт без обратной связи.
    order_delivery_hard_timeout_seconds: int = Field(default=600, ge=300, le=1800)

    chat_autogreeting_enabled: bool = True
    chat_greeting_cooldown_hours: int = Field(default=24, ge=1)
    chat_help_triggers: str = "!help,!помощь,!support,!оператор,!sos,!админ"
    # Cooldown между help-ack'ами в одном чате (0 = выключено).
    # По умолчанию 0: каждое !помощь даёт ответ покупателю и нотификацию
    # владельцу в Telegram. Если хочешь не флудить — поставь >0.
    chat_help_cooldown_seconds: int = Field(default=0, ge=0)
    # После покупки !помощь не останавливает автовыдачу сразу: даём боту
    # время завершить нормальную выдачу. Если товар не выдан после этого окна,
    # заказ уходит в manual_hold и оператор решает вручную.
    chat_help_auto_delivery_grace_seconds: int = Field(default=420, ge=0)
    # Если продавец вручную написал в чат после оплаты, автодоставка по этому
    # чату блокируется. Окно нужно для поздно увиденных заказов: если order
    # event пришёл после ручного сообщения, бот всё равно не должен выдавать дубль.
    order_manual_intervention_guard_seconds: int = Field(default=7200, ge=0)
    work_hours_start: int = Field(default=12, ge=0, le=23)
    work_hours_end: int = Field(default=23, ge=1, le=24)
    seller_display_name: str = "продавец"

    # FunPayAPI.Runner.listen() в новых версиях FunPay часто валит шумные
    # ошибки вида «Не удалось получить истории чатов […]: превышено
    # количество попыток» — это её внутренний рейт-лимит на запросы
    # истории чатов; для наших целей бесполезно, поскольку наш poll-loop
    # сам тянет сообщения через свой HTTP-клиент.
    # По умолчанию выключаем listen-loop, чтобы не загрязнять логи.
    funpay_listen_enabled: bool = False
    funpay_poll_interval_seconds: float = Field(default=5.0, ge=1.0)
    # Дополнительно к preview/unread watcher каждый poll ограниченно
    # проверяет верхние активные чаты с уже известным БД-курcором. Это ловит
    # повторные одинаковые сообщения вроде "!помощь" -> "!помощь", когда
    # preview визуально не меняется.
    funpay_active_chats_poll_limit: int = Field(default=5, ge=0, le=20)

    @field_validator("ns_api_secret")
    @classmethod
    def _check_secret_base64(cls, v: SecretStr) -> SecretStr:
        import base64

        try:
            base64.b64decode(v.get_secret_value(), validate=True)
        except Exception as exc:
            raise ValueError(f"NS_API_SECRET должен быть валидным base64: {exc}") from exc
        return v

    @field_validator("ns_totp_secret")
    @classmethod
    def _check_totp_secret_base32(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return None
        raw = v.get_secret_value().strip().replace(" ", "").upper()
        if not raw:
            return None
        import base64

        try:
            base64.b32decode(raw + "=" * ((-len(raw)) % 8))
        except Exception as exc:
            raise ValueError(
                f"NS_TOTP_SECRET должен быть валидным base32 (буквы A-Z и цифры 2-7): {exc}"
            ) from exc
        return SecretStr(raw)

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def log_path(self) -> Path:
        return PROJECT_ROOT / self.log_dir

    @property
    def data_path(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def help_trigger_set(self) -> set[str]:
        """Список help-триггеров, нормализованных (нижний регистр)."""
        return {
            token.strip().lower()
            for token in self.chat_help_triggers.split(",")
            if token.strip()
        }

    @property
    def telegram_proxy_url(self) -> str | None:
        if not self.telegram_use_proxy:
            return None
        if not self.telegram_proxy_host or not self.telegram_proxy_port:
            return None
        scheme = self.telegram_proxy_type.value
        if self.telegram_proxy_username and self.telegram_proxy_password:
            user = self.telegram_proxy_username.get_secret_value()
            pwd = self.telegram_proxy_password.get_secret_value()
            return f"{scheme}://{user}:{pwd}@{self.telegram_proxy_host}:{self.telegram_proxy_port}"
        return f"{scheme}://{self.telegram_proxy_host}:{self.telegram_proxy_port}"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Singleton-доступ к настройкам."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
        _settings.log_path.mkdir(parents=True, exist_ok=True)
        _settings.data_path.mkdir(parents=True, exist_ok=True)
    return _settings
