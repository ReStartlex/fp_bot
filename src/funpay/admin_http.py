"""
Прямой HTTP-клиент для админских операций FunPay (редактирование лотов).

Зачем он нужен. В установленной версии FunPayAPI методы
`Account.get_lot_fields(lot_id)` и `Account.save_lot(...)` ожидают,
что FunPay вернёт JSON, и парсят ответ как JSON. Реальный FunPay
отдаёт HTML-страницу формы /lots/offerEdit?node=...&offer=...
— отсюда вечная ошибка `JSONDecodeError: Expecting value`.

Здесь мы делаем всё сами:
  1. GET /lots/offerEdit — забираем HTML формы.
  2. BeautifulSoup парсит <input>/<select>/<textarea> в dict.
  3. Меняем нужные поля (цена, остаток, описание...).
  4. POST /lots/offerSave с form-data — FunPay принимает либо HTML,
     либо JSON-ответ с {"msg": ...}. Оба варианта обрабатываем.

Авторизация: достаточно одного `golden_key` (проверено probe-тестом).
PHPSESSID FunPay выдаёт сам через Set-Cookie на первом запросе.

Класс асинхронный: внутри использует sync `requests`, обёрнутые в
`asyncio.to_thread` (тяжёлый HTTP не блокирует event loop).
"""
from __future__ import annotations

import asyncio
import threading
import time as _time_module
from dataclasses import dataclass, field
from typing import Any
import re

import requests
from bs4 import BeautifulSoup
from loguru import logger


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class LotFields:
    """
    Поля админ-формы лота. Хранит ВСЕ поля формы (включая скрытые
    csrf_token, deleted, node_id и т.д.), а так же удобные шорткаты
    для тех полей, что мы реально меняем.
    """
    lot_id: int
    node_id: int | None
    raw_fields: dict[str, str] = field(default_factory=dict)
    title: str | None = None
    public_link: str | None = None

    # удобные сеттеры/геттеры
    @property
    def price(self) -> float | None:
        v = self.raw_fields.get("price")
        try:
            return float(v) if v else None
        except (TypeError, ValueError):
            return None

    @price.setter
    def price(self, value: float | int) -> None:
        # FunPay принимает дробные через точку
        self.raw_fields["price"] = f"{float(value):.2f}"

    @property
    def amount(self) -> int | None:
        v = self.raw_fields.get("amount")
        try:
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    @amount.setter
    def amount(self, value: int | None) -> None:
        if value is None or value == "":
            self.raw_fields["amount"] = ""
        else:
            self.raw_fields["amount"] = str(int(value))

    @property
    def active(self) -> bool:
        return self.raw_fields.get("active") in ("on", "1", "true")

    @active.setter
    def active(self, value: bool) -> None:
        if value:
            self.raw_fields["active"] = "on"
        else:
            # для деактивации FunPay ожидает ОТСУТСТВИЕ ключа active
            self.raw_fields.pop("active", None)

    @property
    def deactivate_after_sale(self) -> bool:
        return self.raw_fields.get("deactivate_after_sale") in ("on", "1", "true")

    @deactivate_after_sale.setter
    def deactivate_after_sale(self, value: bool) -> None:
        if value:
            self.raw_fields["deactivate_after_sale"] = "on"
        else:
            self.raw_fields.pop("deactivate_after_sale", None)


class _RateLimiter:
    """
    Глобальный rate-limiter для исходящих FunPay HTTP-запросов.

    Зачем:
        FunPay активно отдаёт 429 (rate-limit), когда мы шлём слишком
        много запросов параллельно (видно в проде: 4 разных URL получили
        429 в первые секунды после старта). 429-retry с backoff'ом
        компенсирует ошибку постфактум, а RateLimiter — ПРЕДОТВРАЩАЕТ её.

    Два ограничения работают одновременно:
      * max_concurrent — semaphore: сколько HTTP-запросов могут идти
        в один и тот же момент (защита от burst'ов из sync_stock).
      * min_interval_seconds — пауза между ЛЮБЫМИ двумя запросами,
        даже если concurrent=1. Это сглаживает RPS.

    Threadsafe: использует `threading.BoundedSemaphore` + `threading.Lock`,
    потому что `_sync_get/_sync_post` бегают в `asyncio.to_thread(...)`,
    т.е. в worker-потоках. Блокирует только поток, не event-loop.

    Используется как контекст-менеджер:
        with self._rate_limiter.acquire():
            r = self._session.get(url, ...)

    ВАЖНО: оборачивает ТОЛЬКО сам HTTP-вызов, не весь retry-цикл.
    Если бы мы держали acquire во время `time.sleep(backoff_after_429)`,
    то один поток на 30 секунд блокировал бы все остальные. Сейчас
    после 429 мы release'им слот, спим, потом снова acquire — другие
    потоки в это время тоже могут попытаться и тоже огребут 429
    (но min_interval уже их притормозит).
    """
    __slots__ = ("_sem", "_interval", "_last_at", "_interval_lock")

    def __init__(self, max_concurrent: int, min_interval_seconds: float):
        # value=0 был бы deadlock'ом — clamp в минимум 1
        self._sem = threading.BoundedSemaphore(value=max(1, int(max_concurrent)))
        self._interval = max(0.0, float(min_interval_seconds))
        self._last_at = 0.0
        self._interval_lock = threading.Lock()

    def acquire(self) -> "_RateLimiterCtx":
        return _RateLimiterCtx(self)


class _RateLimiterCtx:
    """Контекст-менеджер для _RateLimiter (см. _RateLimiter.acquire)."""
    __slots__ = ("_lim",)

    def __init__(self, lim: _RateLimiter) -> None:
        self._lim = lim

    def __enter__(self) -> "_RateLimiterCtx":
        # 1. Ждём свободный слот (max_concurrent).
        self._lim._sem.acquire()
        # 2. Соблюдаем минимальный интервал между запросами.
        if self._lim._interval > 0:
            with self._lim._interval_lock:
                now = _time_module.monotonic()
                wait = (self._lim._last_at + self._lim._interval) - now
                if wait > 0:
                    _time_module.sleep(wait)
                self._lim._last_at = _time_module.monotonic()
        return self

    def __exit__(self, *_exc) -> None:
        # Семафор отпускаем ВСЕГДА, даже если внутри было исключение.
        self._lim._sem.release()


class FunPayAdminClient:
    """
    Прямой клиент FunPay для admin-операций с лотами.

    Использование:
        admin = FunPayAdminClient(golden_key=..., phpsessid=...)
        fields = await admin.get_lot_fields(lot_id=69300023)
        fields.price = 158
        fields.amount = 10
        await admin.save_lot(fields)
    """

    BASE = "https://funpay.com"

    def __init__(
        self,
        golden_key: str,
        phpsessid: str | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        *,
        max_429_retries: int = 4,
        base_429_backoff_seconds: float = 1.0,
        max_429_backoff_seconds: float = 30.0,
        max_5xx_retries: int = 2,
        rate_max_concurrent: int = 4,
        rate_min_interval_seconds: float = 0.1,
    ) -> None:
        if not golden_key:
            raise ValueError("FunPayAdminClient: golden_key обязателен")
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._session.headers["Accept-Language"] = "ru,en;q=0.9"
        # кладём cookies в сессию явно (с правильным доменом)
        self._session.cookies.set("golden_key", golden_key, domain="funpay.com")
        if phpsessid:
            self._session.cookies.set(
                "PHPSESSID", phpsessid, domain="funpay.com"
            )
        self._golden_key = golden_key
        self._user_agent = user_agent
        self._csrf_token: str | None = None
        # параметры backoff'а на 429
        self._max_429_retries = max(0, int(max_429_retries))
        self._base_429_backoff = max(0.0, float(base_429_backoff_seconds))
        self._max_429_backoff = max(self._base_429_backoff, float(max_429_backoff_seconds))
        # параметры backoff'а на 5xx / сетевые ошибки (base/max общий с 429)
        self._max_5xx_retries = max(0, int(max_5xx_retries))
        # глобальный rate-limiter (предотвращает 429 ДО его возникновения).
        # Применяется и к GET (_sync_get), и к POST (_sync_post).
        self._rate_limiter = _RateLimiter(
            max_concurrent=rate_max_concurrent,
            min_interval_seconds=rate_min_interval_seconds,
        )
        # HTTP-метрики (observability):
        #   ok          — успешных запросов (2xx/redirect, не считая retry-промежуточных)
        #   retry_429   — сколько раз пришлось ждать после 429 и retry'ить
        #   retry_5xx   — сколько раз пришлось ждать после 502/503/504 (или сетевой) и retry'ить
        #   exhausted   — сколько раз ВСЕ retry исчерпались (это и есть пропущенный лот)
        # Снимаются методом get_and_reset_http_metrics() — атомарно
        # (под self._metrics_lock), чтобы между чтением и сбросом не
        # потерять инкременты из параллельных потоков.
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, int] = {
            "ok": 0,
            "retry_429": 0,
            "retry_5xx": 0,
            "exhausted": 0,
        }

    def _metrics_inc(self, key: str, by: int = 1) -> None:
        """Thread-safe инкремент счётчика метрик."""
        with self._metrics_lock:
            self._metrics[key] = self._metrics.get(key, 0) + by

    def get_and_reset_http_metrics(self) -> dict[str, int]:
        """
        Атомарно снимает текущие метрики и обнуляет их.

        Возвращает snapshot ВСЕХ ключей (ok, retry_429, retry_5xx, exhausted).
        Используется sync_stock в конце каждого цикла, чтобы залогировать
        агрегат http-нагрузки за цикл — это даёт прямую видимость работы
        rate-limiter'a в проде (раньше приходилось grep'ать journalctl).
        """
        with self._metrics_lock:
            snap = dict(self._metrics)
            for k in self._metrics:
                self._metrics[k] = 0
            return snap

    # ----- low-level -----

    @staticmethod
    def _compute_429_backoff(
        attempt: int,
        retry_after_header: str | None,
        base_seconds: float,
        max_seconds: float,
    ) -> float:
        """
        Сколько секунд спать ПЕРЕД попыткой номер (attempt+1).

        Логика:
          1. Если FunPay прислал Retry-After и это парсится как
             число секунд (>= 0) — берём его, но не больше `max_seconds`.
          2. Иначе exponential backoff: base * 2^attempt, capped до max.

        HTTP-date в Retry-After не поддерживаем (FunPay использует только
        числовой формат) — fallback на exponential.
        """
        if retry_after_header:
            ra = str(retry_after_header).strip()
            try:
                seconds = float(ra)
                if seconds >= 0:
                    return min(seconds, max_seconds)
            except (TypeError, ValueError):
                pass
        delay = base_seconds * (2 ** max(0, int(attempt)))
        return min(delay, max_seconds)

    # Статус-коды, которые ретраим на GET. Объясняется в _sync_get:
    #   - 429: rate-limit; FunPay просит «подожди, я перегружен».
    #   - 502/503/504: transient gateway issues — FunPay сам не отдаёт
    #     наш бекенд (Cloudflare/балансер ловит ошибку). Обычно
    #     рассасывается за 1-5 секунд.
    # 500 НЕ ретраим: это application bug у FunPay, повтор бесполезен.
    # 4xx (404/401/403) тоже не ретраим — это уже наши проблемы (нет
    # лота, нет авторизации) и повтор только пожжёт rate-limit.
    _RETRYABLE_GET_STATUSES_TRANSIENT: frozenset[int] = frozenset({502, 503, 504})

    def _sync_get(self, url: str, retries: int | None = None) -> requests.Response:
        """
        GET к FunPay с обработкой rate-limit (429) и transient 5xx.

        Стратегия:
        - 429: отдельный счётчик ретраев (self._max_429_retries),
          уважает Retry-After. Раньше один 429 пропускал лот в sync_stock.
        - 502/503/504 (и сетевые ошибки): отдельный счётчик
          (self._max_5xx_retries, обычно ниже — FunPay 5xx или
          совсем кратковременный, или достаточно «глубокий» чтобы
          не было смысла долго ждать).
        - 500/4xx: НЕ ретраим, отдаём исключение.

        Параметр `retries` (если задан) переопределяет только 429-счётчик
        — это сделано для обратной совместимости со старым API.

        Backoff: exponential, общая логика `_compute_429_backoff`.
        """
        import time as _time
        max_429 = self._max_429_retries if retries is None else int(retries)
        max_5xx = self._max_5xx_retries
        attempts_429 = 0
        attempts_5xx = 0
        last_exc: Exception | None = None
        last_response: requests.Response | None = None

        # Жёсткий потолок на общее число итераций — защита от
        # потенциального race condition / бага в счётчиках.
        hard_cap = max_429 + max_5xx + 2

        for _ in range(hard_cap + 1):
            try:
                with self._rate_limiter.acquire():
                    r = self._session.get(url, timeout=20, allow_redirects=True)
            except Exception as exc:
                last_exc = exc
                if attempts_5xx < max_5xx:
                    # сетевые ошибки трактуем как 5xx «server unreachable»
                    delay = self._compute_429_backoff(
                        attempts_5xx, None,
                        self._base_429_backoff, self._max_429_backoff,
                    )
                    logger.warning(
                        f"FunPay GET network error: {type(exc).__name__}: {exc}; "
                        f"attempt {attempts_5xx + 1}/{max_5xx + 1}, "
                        f"backoff {delay:.2f}s — {url[:80]}"
                    )
                    _time.sleep(delay)
                    attempts_5xx += 1
                    self._metrics_inc("retry_5xx")
                    continue
                self._metrics_inc("exhausted")
                raise

            last_response = r

            if r.status_code == 429:
                if attempts_429 < max_429:
                    ra = r.headers.get("Retry-After")
                    delay = self._compute_429_backoff(
                        attempts_429, ra,
                        self._base_429_backoff, self._max_429_backoff,
                    )
                    logger.warning(
                        f"FunPay GET 429 (attempt {attempts_429 + 1}/{max_429 + 1}, "
                        f"Retry-After={ra}), backoff {delay:.2f}s — "
                        f"{url[:80]}"
                    )
                    _time.sleep(delay)
                    attempts_429 += 1
                    self._metrics_inc("retry_429")
                    continue
                logger.error(
                    f"FunPay GET 429 после {max_429 + 1} попыток — "
                    f"{url[:80]}"
                )
                self._metrics_inc("exhausted")
                r.raise_for_status()
                return r

            if r.status_code in self._RETRYABLE_GET_STATUSES_TRANSIENT:
                if attempts_5xx < max_5xx:
                    ra = r.headers.get("Retry-After")
                    delay = self._compute_429_backoff(
                        attempts_5xx, ra,
                        self._base_429_backoff, self._max_429_backoff,
                    )
                    logger.warning(
                        f"FunPay GET {r.status_code} (attempt "
                        f"{attempts_5xx + 1}/{max_5xx + 1}, Retry-After={ra}), "
                        f"backoff {delay:.2f}s — {url[:80]}"
                    )
                    _time.sleep(delay)
                    attempts_5xx += 1
                    self._metrics_inc("retry_5xx")
                    continue
                logger.error(
                    f"FunPay GET {r.status_code} после {max_5xx + 1} попыток "
                    f"— {url[:80]}"
                )
                self._metrics_inc("exhausted")
                r.raise_for_status()
                return r

            # 200 / 3xx / не-retryable 4xx-5xx (404, 500, ...) — отдаём как есть.
            # Только успешные (2xx) считаем как ok.
            if 200 <= r.status_code < 400:
                self._metrics_inc("ok")
            r.raise_for_status()
            return r

        # На случай совсем странного зацикливания (hard_cap исчерпан).
        if last_exc is not None:
            raise last_exc
        if last_response is not None:
            last_response.raise_for_status()
            return last_response
        raise RuntimeError(f"_sync_get({url}): retries exhausted")

    def _sync_post(self, url: str, data: dict[str, str]) -> requests.Response:
        """
        Один POST формы — без ретраев. Используется как примитив.

        Под глобальным rate-limiter'ом (тот же что и у GET): FunPay
        считает совокупный RPS, POST к offerSave участвует в нём
        наравне с GET к offerEdit/chat.

        Метрики: ok инкрементим только для 2xx/3xx (429/5xx считаются
        в `_sync_post_form_with_429_retry`, чтобы не двоить retry-метрики).
        """
        with self._rate_limiter.acquire():
            r = self._session.post(
                url,
                data=data,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{self.BASE}/lots/offerEdit",
                    "Origin": self.BASE,
                },
                timeout=20,
                allow_redirects=False,
            )
        if 200 <= r.status_code < 400:
            self._metrics_inc("ok")
        return r

    def _sync_post_form_with_429_retry(
        self,
        url: str,
        data: dict[str, str],
        *,
        retries: int | None = None,
    ) -> requests.Response:
        """
        POST формы /lots/offerSave с retries+backoff на 429.

        Возвращает последнюю Response (даже если она 429) — вызывающий
        сам решает, считать ли это успехом. На сетевые ошибки —
        retry с тем же backoff, на исчерпании retries поднимает
        исходное исключение.
        """
        import time as _time
        max_retries = self._max_429_retries if retries is None else int(retries)
        last_exc: Exception | None = None
        last_response: requests.Response | None = None

        for attempt in range(max_retries + 1):
            try:
                r = self._sync_post(url, data)
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    _time.sleep(
                        self._compute_429_backoff(
                            attempt, None, self._base_429_backoff, self._max_429_backoff
                        )
                    )
                    self._metrics_inc("retry_5xx")
                    continue
                self._metrics_inc("exhausted")
                raise
            last_response = r
            if r.status_code == 429 and attempt < max_retries:
                ra = r.headers.get("Retry-After")
                delay = self._compute_429_backoff(
                    attempt, ra, self._base_429_backoff, self._max_429_backoff
                )
                logger.warning(
                    f"FunPay POST 429 (attempt {attempt + 1}/{max_retries + 1}, "
                    f"Retry-After={ra}), backoff {delay:.2f}s — "
                    f"{url[:80]}"
                )
                _time.sleep(delay)
                self._metrics_inc("retry_429")
                continue
            # либо не 429, либо retries исчерпаны
            if r.status_code == 429:
                # вышли по исчерпанию retries (attempt == max_retries)
                self._metrics_inc("exhausted")
            return r

        if last_exc is not None:
            raise last_exc
        assert last_response is not None  # не должно случиться
        return last_response

    # ----- public API -----

    async def whoami(self) -> dict[str, Any]:
        """Проверка, что cookies живые. Возвращает username/id из главной страницы."""
        r = await asyncio.to_thread(self._sync_get, f"{self.BASE}/")
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.find("body")
        user_id: int | None = None
        username: str | None = None
        if body and body.get("data-user-id"):
            try:
                user_id = int(body["data-user-id"])
            except (TypeError, ValueError):
                pass
        # username из ссылки .user-link-name
        link = soup.select_one(".user-link .user-link-name") or soup.find(
            "a", class_="user-link-name"
        )
        if link:
            username = link.get_text(strip=True)

        # CSRF-токен и app-data — кешируем для последующего send_chat_message
        csrf = None
        meta = soup.find("meta", attrs={"name": "csrf-token"}) or soup.find(
            "input", attrs={"name": "csrf_token"}
        )
        if meta is not None:
            csrf = meta.get("content") or meta.get("value")
        if csrf:
            self._csrf_token = csrf

        return {
            "user_id": user_id,
            "username": username,
            "authenticated": bool(user_id),
            "csrf_token": csrf,
        }

    async def _ensure_csrf(self) -> str | None:
        """
        Возвращает CSRF-токен. Ищет в нескольких источниках:
        кэш → главная → /chat/.
        """
        token = getattr(self, "_csrf_token", None)
        if token:
            return token

        info = await self.whoami()
        token = info.get("csrf_token")
        if token:
            self._csrf_token = token
            return token

        # Fallback: тянем CSRF со страницы /chat/, иногда он только там
        try:
            r = await asyncio.to_thread(self._sync_get, f"{self.BASE}/chat/")
            soup = BeautifulSoup(r.text, "html.parser")
            for sel, attr in (
                ('meta[name="csrf-token"]', "content"),
                ('input[name="csrf_token"]', "value"),
                ('input[name="csrf-token"]', "value"),
            ):
                el = soup.select_one(sel)
                if el is not None:
                    val = el.get(attr)
                    if val:
                        self._csrf_token = val
                        return val
            # Или из embedded JS: window.csrf_token = "..."
            m = re.search(
                r'csrf[_-]token["\']?\s*[:=]\s*["\']([a-zA-Z0-9_\-]+)["\']',
                r.text,
            )
            if m:
                self._csrf_token = m.group(1)
                return m.group(1)
        except Exception:
            pass
        return None

    async def send_chat_message(
        self, chat_id: int, text: str, retries: int = 2
    ) -> dict[str, Any]:
        """
        Прямая отправка сообщения в FunPay-чат через AJAX /runner/.

        Контракт берём ровно из FunPayAPI.Account.send_message — это тот
        же endpoint и payload, который FunPay сейчас поддерживает.

        Отличие от FunPayAPI: мы НЕ парсим html ответа в `Message`
        объект. Достаточно вернуть `{ok: True}` если FunPay принял
        отправку. Парсер html — самая хрупкая часть в FunPayAPI,
        именно он падает с `'NoneType' object has no attribute 'text'`
        когда FunPay меняет вёрстку.

        Retries: на rate-limit / временную сеть. Между попытками — пауза
        с экспоненциальным ростом.
        """
        import json as _json
        last_result: dict[str, Any] = {"ok": False}

        for attempt in range(retries + 1):
            csrf = await self._ensure_csrf()

            request_payload = {
                "action": "chat_message",
                "data": {
                    "node": int(chat_id),
                    "last_message": -1,
                    "content": text,
                },
            }
            # Формат objects берём ровно из FunPayAPI.Account.send_message —
            # он отлично работает на сервере FunPay; если этого блока нет,
            # FunPay часто отвечает {"response": null}.
            objects_payload = [
                {
                    "type": "chat_node",
                    "id": int(chat_id),
                    "tag": "00000000",
                    "data": {
                        "node": int(chat_id),
                        "last_message": -1,
                        "content": "",
                    },
                }
            ]

            data: dict[str, str] = {
                "objects": _json.dumps(objects_payload),
                "request": _json.dumps(request_payload),
            }
            if csrf:
                data["csrf_token"] = csrf

            url = f"{self.BASE}/runner/"

            def _post() -> requests.Response:
                return self._session.post(
                    url,
                    data=data,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": f"{self.BASE}/chat/?node={int(chat_id)}",
                        "Origin": self.BASE,
                        "Accept": "*/*",
                    },
                    timeout=20,
                    allow_redirects=False,
                )

            try:
                r = await asyncio.to_thread(_post)
            except Exception as exc:
                last_result = {
                    "ok": False,
                    "exception": f"{type(exc).__name__}: {exc}",
                    "attempt": attempt,
                }
                if attempt < retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                continue

            result: dict[str, Any] = {
                "http_status": r.status_code,
                "body_preview": r.text[:300],
                "attempt": attempt,
            }
            try:
                j = r.json()
            except Exception:
                j = None
            if isinstance(j, dict):
                result["json"] = j
                response = j.get("response") or {}
                error = response.get("error") if isinstance(response, dict) else None
                result["ok"] = r.ok and not error
                if error:
                    result["funpay_error"] = error
                # На 429 / временную ошибку FunPay часто отвечает 200 + error
                if not result["ok"] and attempt < retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    last_result = result
                    continue
                return result
            # Не JSON
            result["ok"] = bool(r.ok)
            if not r.ok:
                result["funpay_error"] = f"HTTP {r.status_code}"
                if attempt < retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    last_result = result
                    continue
            return result

        return last_result

    async def get_chats_snapshot(self) -> list[dict[str, Any]]:
        """
        Тянет страницу /chat/ и парсит список чатов из левой панели.

        FunPay UI на /chat/ показывает список из ~50 последних чатов с
        собеседниками. Каждая карточка — <a class="contact-item"> с
        атрибутами:
            - data-id (или href ?node=NODE_ID) — chat_id
            - .media-user-name — username собеседника
            - .contact-item-message — текст последнего сообщения (превью)
            - .contact-item-time — время или подпись «нет сообщений»

        Возвращает list[dict] с полями:
            chat_id (int), username (str|None), preview (str), node (int)
        """
        r = await asyncio.to_thread(self._sync_get, f"{self.BASE}/chat/")
        soup = BeautifulSoup(r.text, "html.parser")

        items: list[dict[str, Any]] = []
        # FunPay рендерит карточки через <a class="contact-item"> или
        # <a class="contact-item js-contact-item">; data-id содержит id чата.
        for a in soup.select("a.contact-item, a.js-contact-item"):
            data_id = a.get("data-id") or ""
            href = a.get("href") or ""
            chat_id: int | None = None
            # 1. data-id
            try:
                chat_id = int(data_id) if data_id else None
            except (TypeError, ValueError):
                chat_id = None
            # 2. href ?node=...
            if chat_id is None and "node=" in href:
                m = re.search(r"node=(\d+)", href)
                if m:
                    try:
                        chat_id = int(m.group(1))
                    except (TypeError, ValueError):
                        pass
            if chat_id is None:
                continue

            username_el = a.select_one(".media-user-name, .contact-item-name")
            username = username_el.get_text(strip=True) if username_el else None

            preview_el = a.select_one(
                ".contact-item-message, .contact-item-text, .contact-item-msg"
            )
            preview = preview_el.get_text(strip=True) if preview_el else ""

            class_text = " ".join(str(c) for c in (a.get("class") or []))
            unread = (
                bool(re.search(r"unread|new", class_text, flags=re.IGNORECASE))
                or bool(a.find(class_=re.compile(r"unread|new", re.IGNORECASE)))
                or bool(
                    a.select_one(
                        ".badge, .badge-counter, .counter, "
                        ".contact-item-unread, .unread"
                    )
                )
            )

            items.append(
                {
                    "chat_id": chat_id,
                    "username": username,
                    "preview": preview,
                    "unread": unread,
                }
            )
        return items

    async def get_chat_messages(
        self, chat_id: int, *, last_id: int | None = None
    ) -> list[dict[str, Any]]:
        """
        Парсит сообщения из чата /chat/?node=CHAT_ID.

        Возвращает список словарей вида:
            {message_id, author_id, author_username, text, is_my, when}
        Самое свежее — в конце списка (по порядку на странице FunPay).

        Парсер устойчив к разным версиям FunPay-HTML:
        - сообщение ищется в любом из контейнеров `.chat-msg-item`,
          `.chat-msg`, `.chat-message`, `.message`;
        - message_id ищется в data-id, id="msg-NNN" или в любом
          атрибуте, содержащем число;
        - текст сообщения берётся из `.chat-msg-text`/`.message-text`/
          `.chat-msg-body` или, если их нет, — из самого узла после
          вычитания author-link/timestamp.
        """
        url = f"{self.BASE}/chat/?node={int(chat_id)}"
        r = await asyncio.to_thread(self._sync_get, url)
        soup = BeautifulSoup(r.text, "html.parser")

        out: list[dict[str, Any]] = []
        # Расширенный список селекторов — FunPay периодически меняет
        # классы темы. Главное: один и тот же узел не сматчится дважды,
        # потому что мы дедупим по выраженному id внутри узла.
        message_nodes = soup.select(
            ".chat-msg-item, .chat-msg, .chat-message, "
            ".message-item, .message"
        )
        seen_ids: set[int] = set()
        for el in message_nodes:
            mid = self._extract_message_id(el)
            if mid is not None:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            if last_id is not None and mid is not None and mid <= last_id:
                continue

            author_id = self._extract_author_id(el)
            author_username = self._extract_author_username(el)
            text = self._extract_message_text(el)
            if not text:
                continue

            out.append(
                {
                    "message_id": mid,
                    "author_id": author_id,
                    "author_username": author_username,
                    "text": text,
                }
            )
        return out

    @staticmethod
    def _extract_message_id(el: Any) -> int | None:
        """Достаёт message_id из узла любыми доступными способами."""
        for attr in ("data-id", "data-message-id", "data-msg-id"):
            v = el.get(attr)
            if v:
                m = re.search(r"\d+", str(v))
                if m:
                    try:
                        return int(m.group(0))
                    except ValueError:
                        pass
        # id="msg-12345" / id="message-12345"
        node_id = el.get("id") or ""
        m = re.search(r"\d+", node_id)
        if m:
            try:
                return int(m.group(0))
            except ValueError:
                pass
        return None

    @staticmethod
    def _extract_author_id(el: Any) -> int | None:
        for attr in ("data-author", "data-user-id", "data-author-id"):
            v = el.get(attr)
            if v:
                try:
                    return int(re.sub(r"\D", "", str(v)))
                except (TypeError, ValueError):
                    pass
        # Иногда автор зашит во вложенный <a data-id="USER_ID">
        link = el.select_one("a[data-user-id], a[data-id]")
        if link is not None:
            v = link.get("data-user-id") or link.get("data-id")
            if v:
                try:
                    return int(re.sub(r"\D", "", str(v)))
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _extract_author_username(el: Any) -> str | None:
        for sel in (
            ".chat-msg-author-link",
            "a.media-user-name",
            ".chat-msg-author",
            ".message-author",
            ".chat-msg-username",
        ):
            link = el.select_one(sel)
            if link is not None:
                text = link.get_text(strip=True)
                if text:
                    return text
        return None

    @staticmethod
    def _extract_message_text(el: Any) -> str:
        """
        Достаёт текст сообщения. Сначала через явный body-селектор,
        если нет — берём весь текст узла минус ссылку на автора и
        timestamp.
        """
        for sel in (
            ".chat-msg-text",
            ".message-text",
            ".chat-msg-body",
            ".message-body",
            ".chat-msg-content",
        ):
            body_el = el.select_one(sel)
            if body_el is not None:
                text = body_el.get_text(separator=" ", strip=True)
                if text:
                    return text

        # Fallback: целиком текст узла, выкидывая author/time.
        copy = BeautifulSoup(str(el), "html.parser")
        for junk_sel in (
            ".chat-msg-author-link",
            ".chat-msg-author",
            "a.media-user-name",
            ".chat-msg-username",
            ".chat-msg-date",
            ".chat-msg-time",
            ".message-time",
            ".message-date",
            "time",
        ):
            for j in copy.select(junk_sel):
                j.decompose()
        return copy.get_text(separator=" ", strip=True)

    async def get_lot_fields(
        self, lot_id: int, node_id: int | None = None
    ) -> LotFields:
        """
        Забирает HTML-форму редактирования и превращает её в LotFields.

        FunPay принимает URL в двух вариантах:
          /lots/offerEdit?offer=LOT_ID
          /lots/offerEdit?node=NODE_ID&offer=LOT_ID&location=offer
        Если node_id известен — добавляем (быстрее находит). Иначе
        FunPay сам редиректит на нужную страницу.
        """
        params = []
        if node_id is not None:
            params.append(f"node={int(node_id)}")
        params.append(f"offer={int(lot_id)}")
        params.append("location=offer")
        url = f"{self.BASE}/lots/offerEdit?" + "&".join(params)

        r = await asyncio.to_thread(self._sync_get, url)
        soup = BeautifulSoup(r.text, "html.parser")

        # FunPay могут уводить на logged-out страницу — проверим
        if soup.find("form", action=re.compile(r"/account/login")):
            raise FunPayAuthError(
                f"FunPay перебросил на форму логина на {url}. "
                f"Похоже, golden_key инвалидирован — обнови его в .env."
            )

        # Ищем главную форму редактирования
        form = (
            soup.find("form", action=re.compile(r"/lots/offerSave"))
            or soup.find("form", id="lots-offer-edit")
            or soup.find("form", class_="js-lots-edit")
        )
        if form is None:
            # fallback — найдём по наличию input[name="offer_id"]
            offer_input = soup.find("input", attrs={"name": "offer_id"})
            if offer_input is not None:
                form = offer_input.find_parent("form")
        if form is None:
            preview = r.text[:300].replace("\n", " ")
            raise FunPayParseError(
                f"Не нашёл форму редактирования лота в HTML {url}. "
                f"HTML preview: {preview!r}"
            )

        raw: dict[str, str] = {}
        # <input>
        for el in form.find_all("input"):
            name = el.get("name")
            if not name:
                continue
            itype = (el.get("type") or "text").lower()
            if itype in ("submit", "button"):
                continue
            if itype in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    raw[name] = el.get("value") or "on"
                # неотмеченный чекбокс — не отправляем
                continue
            raw[name] = el.get("value") or ""

        # <select>
        for el in form.find_all("select"):
            name = el.get("name")
            if not name:
                continue
            selected = el.find("option", selected=True)
            if selected is None:
                # FunPay по умолчанию первый option
                selected = el.find("option")
            raw[name] = selected.get("value", "") if selected else ""

        # <textarea>
        for el in form.find_all("textarea"):
            name = el.get("name")
            if not name:
                continue
            raw[name] = el.get_text() or ""

        # Извлекаем node_id и title для удобства
        resolved_node_id = node_id
        if resolved_node_id is None:
            for n in ("node_id", "game", "subcategory"):
                v = raw.get(n)
                try:
                    resolved_node_id = int(v) if v else resolved_node_id
                except (TypeError, ValueError):
                    pass

        title_el = soup.find("h1") or soup.find("title")
        title = title_el.get_text(strip=True) if title_el else None

        return LotFields(
            lot_id=lot_id,
            node_id=resolved_node_id,
            raw_fields=raw,
            title=title,
        )

    async def save_lot(self, lot: LotFields) -> dict[str, Any]:
        """
        Сохраняет лот. Возвращает dict с диагностикой (status, body_preview).

        FunPay /lots/offerSave принимает form-data, отвечает либо
        JSON {"msg": "ok"|"...error..."}, либо HTML-страницу
        (если что-то пошло сильно не так).

        На 429 (rate-limit от FunPay) делаем retries+backoff
        (см. `_sync_post_form_with_429_retry`) — раньше один 429
        заставлял sync_stock полностью пропустить лот.
        """
        url = f"{self.BASE}/lots/offerSave"
        data = dict(lot.raw_fields)
        # offer_id должен быть в данных
        data.setdefault("offer_id", str(lot.lot_id))

        r = await asyncio.to_thread(self._sync_post_form_with_429_retry, url, data)

        result: dict[str, Any] = {
            "http_status": r.status_code,
            "content_type": r.headers.get("Content-Type", ""),
            "body_preview": r.text[:300],
        }
        # 429 после всех ретраев — явная диагностика, без HTML-каши в логе
        if r.status_code == 429:
            result["ok"] = False
            result["funpay_error"] = (
                f"FunPay rate-limit 429 после "
                f"{self._max_429_retries + 1} попыток "
                f"(Retry-After={r.headers.get('Retry-After')!r})"
            )
            return result
        # Пробуем распарсить JSON-ответ
        try:
            j = r.json()
        except Exception:
            j = None
        if isinstance(j, dict):
            result["json"] = j
            # msg=ok — успех; msg=что-то ещё — ошибка от FunPay
            msg = (j.get("msg") or "").strip().lower()
            result["ok"] = (msg in ("", "ok", "success")) and r.ok
            if not result["ok"]:
                result["funpay_error"] = j.get("msg") or j
            return result
        # HTML-ответ — успех, только если 200 и нет признаков ошибки
        if r.ok and "ошибк" not in r.text.lower():
            result["ok"] = True
        else:
            result["ok"] = False
            result["funpay_error"] = "Получили HTML, не JSON, и/или статус != 200"
        return result


# ----- ошибки -----

class FunPayAuthError(RuntimeError):
    """golden_key/PHPSESSID невалидны на стороне FunPay."""


class FunPayParseError(RuntimeError):
    """Не получилось распарсить HTML формы лота."""
