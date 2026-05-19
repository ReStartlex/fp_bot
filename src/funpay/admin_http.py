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

    # ----- low-level -----

    def _sync_get(self, url: str, retries: int = 2) -> requests.Response:
        """
        GET к FunPay с обработкой rate-limit (429).
        FunPay в горячий момент даёт 429 — нужна короткая backoff-пауза.
        """
        import time as _time
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = self._session.get(url, timeout=20, allow_redirects=True)
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    _time.sleep(0.5 * (2 ** attempt))
                    continue
                raise
            if r.status_code == 429:
                # Backoff: 1s, 2s
                if attempt < retries:
                    _time.sleep(1.0 * (2 ** attempt))
                    continue
            r.raise_for_status()
            return r
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"_sync_get({url}): retries exhausted")

    def _sync_post(self, url: str, data: dict[str, str]) -> requests.Response:
        # POST формы — FunPay ожидает application/x-www-form-urlencoded
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
        return r

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
        """
        url = f"{self.BASE}/lots/offerSave"
        data = dict(lot.raw_fields)
        # offer_id должен быть в данных
        data.setdefault("offer_id", str(lot.lot_id))

        r = await asyncio.to_thread(self._sync_post, url, data)

        result: dict[str, Any] = {
            "http_status": r.status_code,
            "content_type": r.headers.get("Content-Type", ""),
            "body_preview": r.text[:300],
        }
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
