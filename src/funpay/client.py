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

    # Класс-level флаг: один раз monkeypatch на requests.Session.request.
    # Хранит PHPSESSID/golden_key последнего connect() — этого достаточно,
    # потому что в этом приложении единственный аккаунт FunPay.
    _patch_installed: bool = False
    _patched_golden_key: str | None = None
    _patched_phpsessid: str | None = None
    _first_request_logged: bool = False

    @classmethod
    def _install_global_funpay_cookie_patch(cls) -> None:
        """
        Глобально патчит requests.Session.request так, что для любого
        запроса к funpay.com автоматически добавляются cookies
        golden_key и PHPSESSID. Это спасает в случае, когда библиотека
        FunPayAPI (или её форк) держит requests.Session в каком-то
        нестандартном атрибуте, до которого мы не дотягиваемся через
        introspection.

        Патч ставится один раз за жизнь процесса. PHPSESSID/golden_key
        берутся из class-level переменных, которые обновляет connect().
        """
        if cls._patch_installed:
            return

        import requests

        original_request = requests.Session.request

        def patched_request(self, method, url, **kwargs):  # type: ignore[no-redef]
            # Применяем cookies только к запросам FunPay, чтобы не
            # просочиться в посторонние HTTP-вызовы (Telegram, NS, и т.д.).
            try:
                target_funpay = "funpay.com" in str(url).lower()
            except Exception:
                target_funpay = False

            if target_funpay and (cls._patched_golden_key or cls._patched_phpsessid):
                # 1. headers["Cookie"] — САМЫЙ важный путь.
                # FunPayAPI в установленной версии формирует Cookie вручную
                # и кладёт его в headers={"Cookie": "golden_key=..."}.
                # Когда у requests есть и cookies=, и headers["Cookie"] —
                # она использует headers. Поэтому если просто положить
                # PHPSESSID в kwargs["cookies"], он будет проигнорирован.
                # Решение: распарсить существующий Cookie-header,
                # дописать/перезаписать нужные ключи, склеить обратно.
                headers = kwargs.get("headers")
                if headers is None:
                    headers = {}
                elif not isinstance(headers, dict):
                    headers = dict(headers)
                # case-insensitive поиск Cookie
                cookie_key = next(
                    (k for k in headers if k.lower() == "cookie"), "Cookie"
                )
                existing_cookie = headers.get(cookie_key, "") or ""
                parts: dict[str, str] = {}
                for piece in existing_cookie.split(";"):
                    piece = piece.strip()
                    if not piece or "=" not in piece:
                        continue
                    k, _, v = piece.partition("=")
                    parts[k.strip()] = v.strip()
                if cls._patched_golden_key:
                    parts["golden_key"] = cls._patched_golden_key
                if cls._patched_phpsessid:
                    parts["PHPSESSID"] = cls._patched_phpsessid
                headers[cookie_key] = "; ".join(
                    f"{k}={v}" for k, v in parts.items()
                )
                kwargs["headers"] = headers

                # 2. kwargs["cookies"] — дублируем (на случай, если форк
                # библиотеки переключился на cookies kwarg).
                kw_cookies = kwargs.get("cookies")
                if isinstance(kw_cookies, dict) or kw_cookies is None:
                    kw_cookies = dict(kw_cookies or {})
                    if cls._patched_golden_key:
                        kw_cookies.setdefault("golden_key", cls._patched_golden_key)
                    if cls._patched_phpsessid:
                        kw_cookies.setdefault("PHPSESSID", cls._patched_phpsessid)
                    kwargs["cookies"] = kw_cookies

                # 3. self.cookies — на случай нескольких запросов в одной Session
                try:
                    if cls._patched_golden_key:
                        self.cookies.set(
                            "golden_key", cls._patched_golden_key,
                            domain="funpay.com", path="/",
                        )
                    if cls._patched_phpsessid:
                        self.cookies.set(
                            "PHPSESSID", cls._patched_phpsessid,
                            domain="funpay.com", path="/",
                        )
                except Exception:
                    pass

                # 4. диагностика — один раз залогируем первый запрос,
                # чтобы можно было убедиться, что PHPSESSID реально ушёл.
                if not cls._first_request_logged:
                    cls._first_request_logged = True
                    masked = headers[cookie_key]
                    # маскируем значения, оставляем только имена и длину
                    masked_summary = "; ".join(
                        f"{k}=<{len(v)} chars>" for k, v in parts.items()
                    )
                    logger.info(
                        f"FunPay HTTP patch: первый запрос к {url} — "
                        f"Cookie header содержит [{masked_summary}]"
                    )

            return original_request(self, method, url, **kwargs)

        requests.Session.request = patched_request  # type: ignore[assignment]
        cls._patch_installed = True
        logger.info(
            "FunPay HTTP patch установлен (Session.request будет добавлять "
            "golden_key и PHPSESSID во все запросы к funpay.com)"
        )

    async def connect(self) -> Any:
        """
        Подключение к FunPay через golden_key + PHPSESSID.

        Кардинальная стратегия (после нескольких неудачных попыток):
        мы не полагаемся ни на `Account(phpsessid=...)`, ни на атрибут
        `acc.phpsessid`, ни даже на то, что внутри Account найдётся
        атрибут `session`. Вместо этого ставим **глобальный
        monkey-patch на requests.Session.request**, который автоматом
        добавляет cookies для всех запросов к funpay.com. Это работает
        независимо от внутренней архитектуры FunPayAPI.

        Дополнительно вписываем cookies во все найденные `.cookies`
        атрибуты (на случай, если библиотека форсит их при каждом
        запросе) и пробуем acc.phpsessid (некоторые форки читают его).
        """
        from FunPayAPI import Account

        golden_key = self._settings.funpay_golden_key.get_secret_value()
        phpsessid = (
            self._settings.funpay_phpsessid.get_secret_value()
            if self._settings.funpay_phpsessid
            else None
        )

        # Глобальный патч (один раз) + обновляем актуальные значения
        FunPayClient._patched_golden_key = golden_key
        FunPayClient._patched_phpsessid = phpsessid
        FunPayClient._install_global_funpay_cookie_patch()

        def _install_cookies(acc: Any) -> dict[str, Any]:
            """
            Идём по известным местам, где FunPayAPI может прятать
            requests.Session, и вписываем golden_key + PHPSESSID в её
            cookiejar. Возвращаем отчёт, куда именно положили — для лога.
            """
            report: dict[str, Any] = {"sessions_touched": [], "attrs_set": []}

            # 1. атрибут .phpsessid (на случай если форк всё-таки читает его)
            if phpsessid:
                try:
                    setattr(acc, "phpsessid", phpsessid)
                    report["attrs_set"].append("phpsessid")
                except Exception:
                    pass

            # 2. находим все объекты, похожие на requests.Session
            candidates: list[Any] = []
            for owner in (acc, getattr(acc, "runner", None), getattr(acc, "http", None)):
                if owner is None:
                    continue
                for name in (
                    "session", "_session", "sess", "requests_session",
                    "http", "client", "_client",
                ):
                    obj = getattr(owner, name, None)
                    if obj is not None and hasattr(obj, "cookies"):
                        candidates.append((f"{type(owner).__name__}.{name}", obj))
                # сам owner тоже может быть Session-подобным
                if hasattr(owner, "cookies") and hasattr(owner, "get"):
                    candidates.append((f"{type(owner).__name__}", owner))

            for label, sess in candidates:
                try:
                    cookies = sess.cookies
                    # requests.cookies.RequestsCookieJar
                    cookies.set(
                        "golden_key", golden_key, domain="funpay.com", path="/"
                    )
                    if phpsessid:
                        cookies.set(
                            "PHPSESSID", phpsessid, domain="funpay.com", path="/"
                        )
                    # дублируем без domain — некоторые сессии хранят без
                    cookies.set("golden_key", golden_key)
                    if phpsessid:
                        cookies.set("PHPSESSID", phpsessid)
                    # User-Agent через session.headers
                    if hasattr(sess, "headers"):
                        sess.headers["User-Agent"] = self.DEFAULT_USER_AGENT
                    report["sessions_touched"].append(label)
                except Exception as exc:
                    logger.debug(f"FunPay cookie install on {label} failed: {exc}")

            return report

        def _build_and_get() -> tuple[Any, dict[str, Any], dict[str, Any]]:
            ctor_kwargs: dict[str, Any] = {
                "golden_key": golden_key,
                "user_agent": self.DEFAULT_USER_AGENT,
            }
            sig = inspect.signature(Account.__init__)
            if phpsessid:
                if "phpsessid" in sig.parameters:
                    ctor_kwargs["phpsessid"] = phpsessid
                elif "PHPSESSID" in sig.parameters:
                    ctor_kwargs["PHPSESSID"] = phpsessid

            acc = Account(**ctor_kwargs)

            # ставим cookies ДО первого запроса
            report_before = _install_cookies(acc)

            # acc.get() — первый запрос. Просим библиотеку НЕ перетирать
            # PHPSESSID значением из ответа, если такой параметр поддержан.
            try:
                acc.get(update_phpsessid=False)
            except TypeError:
                acc.get()
            except Exception as exc:
                logger.warning(f"FunPay acc.get() упал: {type(exc).__name__}: {exc}")

            # после acc.get() — снова ставим cookies (на случай перетёрки)
            report_after = _install_cookies(acc)
            return acc, report_before, report_after

        async with self._lock:
            self._account, report_before, report_after = await self._to_thread(
                _build_and_get
            )

        # Лог намеренно показывает только факт наличия PHPSESSID, а не его
        # значение — секрет не должен утекать в журналы.
        sessions = sorted(set(
            report_before["sessions_touched"] + report_after["sessions_touched"]
        ))
        has_phpsessid_attr = bool(getattr(self._account, "phpsessid", None))
        logger.info(
            f"FunPay подключён: id={self.account_id}, "
            f"username={self.username}, "
            f"phpsessid={'set' if phpsessid else 'MISSING'}, "
            f"phpsessid_attr={'present' if has_phpsessid_attr else 'absent'}, "
            f"http_sessions_with_cookies={sessions or 'NONE_FOUND'}, "
            f"баланс={self.balance}"
        )
        if phpsessid and not sessions:
            logger.error(
                "FunPay: ни одной requests.Session не найдено в Account. "
                "Cookies некуда положить, библиотека ходит без PHPSESSID. "
                "Запусти `python -m src.tools.funpay_introspect` и пришли "
                "вывод — добавим нужный путь в _install_cookies()."
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
        Лёгкое свойство для логов на старте. В свежей версии FunPayAPI это
        просто атрибут `account.balance` (если он есть) — иначе вернёт None.
        Для боевого получения баланса см. `get_funpay_balance()`.
        """
        for attr in ("total_balance", "balance"):
            value = getattr(self.account, attr, None)
            if value is None or callable(value):
                continue
            if hasattr(value, "total"):
                return getattr(value, "total")
            if hasattr(value, "rub"):
                return getattr(value, "rub")
            return value
        return None

    async def get_funpay_balance(self, lot_id: int | None = None) -> dict[str, Any]:
        """
        Возвращает данные баланса FunPay в виде словаря с привычными полями.

        Account.get_balance(lot_id) в свежей FunPayAPI делает реальный
        HTTP-запрос на страницу указанного лота и парсит блок баланса оттуда.
        Если lot_id не задан — пытаемся взять один из своих лотов.
        """

        def _resolve_lot_id() -> int | None:
            if lot_id is not None:
                return lot_id
            try:
                profile = self.account.get_user(self.account.id)
            except Exception:
                return None
            try:
                lots = profile.get_lots() or []
            except Exception:
                return None
            for lot in lots:
                candidate = getattr(lot, "id", None) or getattr(lot, "lot_id", None)
                if candidate:
                    try:
                        return int(candidate)
                    except (TypeError, ValueError):
                        continue
            return None

        def _call() -> dict[str, Any]:
            target_lot = _resolve_lot_id()
            getter = getattr(self.account, "get_balance", None)
            if not callable(getter):
                return {"error": "Account.get_balance не существует"}
            try:
                if target_lot is None:
                    bal = getter()
                else:
                    bal = getter(target_lot)
            except Exception as exc:
                return {
                    "error": f"{type(exc).__name__}: {exc}",
                    "used_lot_id": target_lot,
                }

            data: dict[str, Any] = {"used_lot_id": target_lot}
            for attr in ("total", "available", "currency", "rub", "usd", "eur"):
                value = getattr(bal, attr, None)
                if value is not None:
                    data[attr] = value
            data["raw_repr"] = repr(bal)
            return data

        return await self._to_thread(_call)

    # ----- Лоты -----

    async def get_my_lots(self) -> list[Any]:
        """
        Возвращает список лотов текущего пользователя.

        В разных версиях FunPayAPI лоты лежат в разных местах:
        - Account.get_lots() / Account.get_my_lots()
        - UserProfile из get_user(my_id): атрибут .lots или метод .get_lots()
        - Иногда лоты — это dict {Subcategory: list[Lot]}, иногда — flat list.
        """

        def _flatten(value: Any) -> list[Any]:
            if value is None:
                return []
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
            return [value]

        def _try_object(obj: Any, methods: list[str]) -> list[Any]:
            for attr in methods:
                value = getattr(obj, attr, None)
                if value is None:
                    continue
                if callable(value):
                    try:
                        value = value()
                    except Exception as exc:
                        logger.debug(f"{type(obj).__name__}.{attr}() упал: {exc}")
                        continue
                flat = _flatten(value)
                if flat:
                    logger.debug(
                        f"get_my_lots: получил {len(flat)} лотов через "
                        f"{type(obj).__name__}.{attr}"
                    )
                    return flat
            return []

        def _get() -> list[Any]:
            acc = self.account

            # 1. Прямые методы на Account
            lots = _try_object(acc, ["get_my_lots", "get_lots", "lots"])
            if lots:
                return lots

            # 2. Через UserProfile
            try:
                profile = acc.get_user(acc.id)
            except Exception as exc:
                logger.warning(f"get_my_lots: account.get_user(self.id) упал: {exc}")
                return []

            lots = _try_object(
                profile,
                ["lots", "get_lots", "get_sorted_lots", "get_lot_pages"],
            )
            if lots:
                return lots

            # 3. Через get_subcategories + перебор
            try:
                if hasattr(profile, "get_subcategories"):
                    subs = profile.get_subcategories()
                    flat = []
                    for sub in (subs or []):
                        sub_lots = _try_object(sub, ["lots", "get_lots"])
                        flat.extend(sub_lots)
                    if flat:
                        logger.debug(
                            f"get_my_lots: получил {len(flat)} лотов через subcategories"
                        )
                        return flat
            except Exception as exc:
                logger.debug(f"profile.get_subcategories() упал: {exc}")

            logger.warning(
                "get_my_lots: ни один путь не сработал. "
                "Запусти `python -m src.tools.funpay_introspect` для разведки."
            )
            return []

        return await self._to_thread(_get)

    async def get_lot_fields(self, lot_id: int) -> Any:
        """
        Поля лота для редактирования (LotFields).

        Распространённая ошибка — `Expecting value: line 1 column 1 (char 0)`:
        это значит, что FunPay вместо JSON отдал HTML страницы логина
        (PHPSESSID протух или golden_key больше не валиден).
        Делаем один reconnect и пробуем снова.
        """
        try:
            return await self._to_thread(self.account.get_lot_fields, lot_id)
        except Exception as exc:
            if not self._looks_like_session_expired(exc):
                raise
            logger.warning(
                f"FunPay get_lot_fields({lot_id}) упал ({type(exc).__name__}: "
                f"{exc}). Похоже, сессия протухла — пробую переподключиться."
            )
            try:
                await self.connect()
            except Exception as exc2:
                logger.error(
                    f"FunPay reconnect упал: {exc2}. Обнови FUNPAY_GOLDEN_KEY "
                    f"и FUNPAY_PHPSESSID в .env и перезапусти сервис."
                )
                raise
            try:
                return await self._to_thread(self.account.get_lot_fields, lot_id)
            except Exception as exc3:
                logger.error(
                    f"FunPay get_lot_fields({lot_id}) и после reconnect упал: "
                    f"{type(exc3).__name__}: {exc3}. Чаще всего это означает, "
                    f"что протух FUNPAY_PHPSESSID. Обнови его в .env (см. "
                    f"deploy/README.md → 'Где взять PHPSESSID') и "
                    f"перезапусти сервис."
                )
                raise

    @staticmethod
    def _looks_like_session_expired(exc: BaseException) -> bool:
        """
        Эвристика: какое из исключений похоже на «сессия FunPay протухла».
        FunPayAPI пытается парсить ответ как JSON, и если получает HTML
        страницы логина — кидает json.decoder.JSONDecodeError или
        ValueError с сообщением «Expecting value: line 1 column 1 (char 0)».
        """
        import json as _json
        if isinstance(exc, _json.JSONDecodeError):
            return True
        text = str(exc).lower()
        return any(p in text for p in (
            "expecting value: line 1 column 1",
            "expecting value: line 1",
            "unauthorized",
            "401",
            "403",
        ))

    async def get_lot_summary(self, lot_id: int) -> dict[str, Any]:
        """
        Удобный нормализованный взгляд на лот: id, описание, цена продавца,
        цена клиента, остаток, активность. Делает 2 запроса (LotShortcut + LotFields),
        чтобы понять обе цены (с комиссией и без).
        """

        def _collect() -> dict[str, Any]:
            data: dict[str, Any] = {"lot_id": lot_id}
            # LotShortcut (видит покупатель) — через мой профиль
            try:
                profile = self.account.get_user(self.account.id)
                for lot in (profile.get_lots() or []):
                    if int(getattr(lot, "id", -1)) == int(lot_id):
                        data["client_price"] = getattr(lot, "price", None)
                        data["description"] = getattr(lot, "description", None)
                        data["title"] = getattr(lot, "title", None)
                        data["public_link"] = getattr(lot, "public_link", None)
                        subcat = getattr(lot, "subcategory", None)
                        if subcat is not None:
                            data["subcategory_id"] = getattr(subcat, "id", None)
                            data["subcategory_name"] = getattr(subcat, "fullname", None) or getattr(
                                subcat, "name", None
                            )
                        break
            except Exception as exc:
                data["shortcut_error"] = str(exc)

            # LotFields (то что я редактирую)
            try:
                fields = self.account.get_lot_fields(lot_id)
                for name in (
                    "price",
                    "amount",
                    "active",
                    "is_active",
                    "deactivate_after_sale",
                    "renewal_days",
                    "stock",
                ):
                    value = getattr(fields, name, None)
                    if value is not None:
                        data[f"fields.{name}"] = value
                # Часть атрибутов лежит в .fields как dict
                inner = getattr(fields, "fields", None)
                if isinstance(inner, dict):
                    data["fields.raw"] = {
                        k: (v[:80] if isinstance(v, str) else v) for k, v in inner.items()
                    }
            except Exception as exc:
                data["fields_error"] = str(exc)

            return data

        return await self._to_thread(_collect)

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
