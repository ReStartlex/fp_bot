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

    markup_percent: float = Field(default=15.0, ge=0)
    funpay_currency: Currency = Currency.RUB
    usd_rub_rate_mode: RateMode = RateMode.AUTO
    usd_rub_rate: float = Field(default=90.0, gt=0)
    price_update_threshold_percent: float = Field(default=2.0, ge=0)
    funpay_stock_cap: int = Field(default=100, ge=1)

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    telegram_enabled: bool = True
    telegram_use_proxy: bool = False
    telegram_proxy_type: ProxyType = ProxyType.SOCKS5
    telegram_proxy_host: str | None = None
    telegram_proxy_port: int | None = None
    telegram_proxy_username: SecretStr | None = None
    telegram_proxy_password: SecretStr | None = None

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "logs"

    funpay_update_rate_limit_per_second: float = Field(default=1.0, gt=0)
    ns_retry_attempts: int = Field(default=3, ge=1)
    ns_retry_delay_seconds: float = Field(default=5.0, gt=0)
    ns_order_poll_interval_seconds: float = Field(default=5.0, gt=0)
    ns_order_timeout_seconds: float = Field(default=600.0, gt=0)

    chat_autogreeting_enabled: bool = True
    chat_greeting_cooldown_hours: int = Field(default=24, ge=1)
    chat_help_triggers: str = "!help,!помощь,!support,!оператор,!sos,!админ"
    work_hours_start: int = Field(default=12, ge=0, le=23)
    work_hours_end: int = Field(default=23, ge=1, le=24)
    seller_display_name: str = "продавец"

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
