"""
Wrapper над неофициальной библиотекой FunPayAPI.

FunPayAPI работает синхронно (requests). Мы оборачиваем её в `to_thread`,
чтобы интегрировать с нашим asyncio-кодом.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, TypeVar

from loguru import logger

from src.config import Settings, get_settings


T = TypeVar("T")


class FunPayClient:
    """
    Минимальный асинхронный wrapper над FunPayAPI.Account.

    Использование:
        async with FunPayClient() as fp:
            await fp.connect()
            info = await fp.account_info()
    """

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._account: Any = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "FunPayClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    @staticmethod
    async def _to_thread(fn: Callable[..., T], *args, **kwargs) -> T:
        """Сахар над `asyncio.to_thread`."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def connect(self) -> Any:
        """
        Подключение к FunPay через golden_key.
        Возвращает объект Account (синхронный).
        """
        from FunPayAPI import Account  # импорт здесь, чтобы import-time не падал

        golden_key = self._settings.funpay_golden_key.get_secret_value()
        phpsessid = (
            self._settings.funpay_phpsessid.get_secret_value()
            if self._settings.funpay_phpsessid
            else None
        )

        def _build_and_get() -> Any:
            kwargs: dict[str, Any] = {
                "golden_key": golden_key,
                "user_agent": self.DEFAULT_USER_AGENT,
            }
            if phpsessid:
                # PHPSESSID параметр может называться по-разному в разных версиях
                sig = inspect.signature(Account.__init__)
                if "phpsessid" in sig.parameters:
                    kwargs["phpsessid"] = phpsessid
                elif "PHPSESSID" in sig.parameters:
                    kwargs["PHPSESSID"] = phpsessid
            acc = Account(**kwargs)
            acc.get()
            return acc

        async with self._lock:
            self._account = await self._to_thread(_build_and_get)
        logger.info(
            f"FunPay подключён: id={self.account_id}, "
            f"username={self.username}, "
            f"баланс={self.balance}"
        )
        return self._account

    @property
    def account(self) -> Any:
        if self._account is None:
            raise RuntimeError("FunPay не подключён. Сначала вызови `await connect()`.")
        return self._account

    # ----- Базовые свойства -----

    @property
    def account_id(self) -> int | None:
        return getattr(self.account, "id", None)

    @property
    def username(self) -> str | None:
        return getattr(self.account, "username", None)

    @property
    def balance(self) -> Any:
        """
        В разных версиях FunPayAPI имя поля баланса различается:
        total_balance / balance / get_balance().
        """
        for attr in ("total_balance", "balance"):
            value = getattr(self.account, attr, None)
            if value is not None:
                return value
        getter = getattr(self.account, "get_balance", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return None

    # ----- Лоты -----

    async def get_my_lots(self) -> list[Any]:
        """
        Возвращает список лотов текущего пользователя (LotShortcut-like).
        В FunPayAPI это берётся через UserProfile самого юзера.
        """

        def _get() -> list[Any]:
            acc = self.account
            profile = acc.get_user(acc.id)
            # В разных версиях имена варьируются
            for attr in ("lots", "get_lots", "get_sorted_lots"):
                value = getattr(profile, attr, None)
                if value is None:
                    continue
                if callable(value):
                    try:
                        value = value()
                    except Exception as exc:
                        logger.debug(f"profile.{attr}() упал: {exc}")
                        continue
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    flat: list[Any] = []
                    for v in value.values():
                        if isinstance(v, list):
                            flat.extend(v)
                        elif isinstance(v, dict):
                            for vv in v.values():
                                if isinstance(vv, list):
                                    flat.extend(vv)
                                else:
                                    flat.append(vv)
                        else:
                            flat.append(v)
                    return flat
            return []

        return await self._to_thread(_get)

    async def get_lot_fields(self, lot_id: int) -> Any:
        """Поля лота для редактирования (LotFields)."""
        return await self._to_thread(self.account.get_lot_fields, lot_id)

    async def save_lot(self, lot_fields: Any) -> None:
        """Сохранить изменения лота (после правки полей)."""
        await self._to_thread(self.account.save_lot, lot_fields)

    async def send_message(self, chat_id: int, text: str) -> Any:
        """Отправить сообщение в чат с покупателем."""
        return await self._to_thread(
            self.account.send_message, chat_id, text
        )

    # ----- Диагностика -----

    def describe_account(self) -> dict[str, Any]:
        """
        Печатает все публичные атрибуты Account. Нужно для разведки:
        какие поля/методы доступны в установленной версии FunPayAPI.
        """
        if self._account is None:
            return {"error": "not connected"}
        result: dict[str, Any] = {}
        for name in sorted(dir(self._account)):
            if name.startswith("_"):
                continue
            try:
                value = getattr(self._account, name)
            except Exception as exc:
                result[name] = f"<error: {exc}>"
                continue
            if callable(value):
                try:
                    sig = str(inspect.signature(value))
                except (ValueError, TypeError):
                    sig = "(...)"
                result[name] = f"<method>{sig}"
            else:
                result[name] = type(value).__name__
        return result
