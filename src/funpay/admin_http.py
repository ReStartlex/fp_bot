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

    # ----- low-level -----

    def _sync_get(self, url: str) -> requests.Response:
        r = self._session.get(url, timeout=20, allow_redirects=True)
        r.raise_for_status()
        return r

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
        return {
            "user_id": user_id,
            "username": username,
            "authenticated": bool(user_id),
        }

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
