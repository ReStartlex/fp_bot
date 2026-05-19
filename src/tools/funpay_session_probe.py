"""
Probe FunPay-сессии — прямой curl-like тест без библиотеки FunPayAPI.

Запуск:
    cd /opt/funpay-ns-bot
    .venv/bin/python -m src.tools.funpay_session_probe

Проверяет три гипотезы:
1) golden_key + PHPSESSID из .env — что отвечает FunPay на главной и
   admin-endpoint редактирования лота.
2) Только golden_key (без PHPSESSID) — может ли FunPay сам выдать сессию
   по long-term токену.
3) Только PHPSESSID (без golden_key) — валиден ли сам PHPSESSID.

Для каждого варианта смотрит, авторизован ли запрос (есть ли username
и нет ли формы логина в ответе).
"""
from __future__ import annotations

import sys
import requests

from src.config import get_settings


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def _looks_authenticated(html: str, expected_username: str | None) -> dict:
    """
    Эвристика: на авторизованных страницах FunPay есть:
    - блок профиля с username
    - ссылка "Выход" (logout)
    - data-user-id
    Если страница содержит форму логина — значит мы гость.
    """
    text = html.lower()
    has_username = bool(
        expected_username and expected_username.lower() in text
    )
    has_logout = ("выход" in text) or ("logout" in text)
    has_user_id = "data-user-id" in text or "user-link" in text
    looks_login = (
        '<form' in text
        and 'action="/account/login"' in text
    ) or "введите ваш логин" in text
    return {
        "authenticated": (has_username or has_logout or has_user_id)
        and not looks_login,
        "username_found": has_username,
        "logout_link": has_logout,
        "login_form": looks_login,
    }


def probe(label: str, cookies: dict[str, str], url: str, expected_username: str) -> None:
    print(f"\n── {label} → {url} ──")
    try:
        r = requests.get(
            url,
            headers={
                "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=False,
            timeout=15,
        )
    except Exception as exc:
        print(f"  ERROR: {type(exc).__name__}: {exc}")
        return

    print(f"  HTTP: {r.status_code}; Content-Type: {r.headers.get('Content-Type')}")
    print(f"  Length: {len(r.text)} bytes")
    if r.headers.get("Location"):
        print(f"  Redirect: {r.headers['Location']}")
    print(f"  Set-Cookie: {r.headers.get('Set-Cookie', '(none)')[:140]}")
    flags = _looks_authenticated(r.text, expected_username)
    print(f"  Признаки авторизации: {flags}")
    snippet = r.text[:240].replace("\n", " ")
    print(f"  HTML preview: {snippet!r}")


def main() -> int:
    s = get_settings()
    gk = s.funpay_golden_key.get_secret_value() if s.funpay_golden_key else ""
    ps = s.funpay_phpsessid.get_secret_value() if s.funpay_phpsessid else ""
    expected = "lol228822"

    print(f"golden_key length:  {len(gk)}")
    print(f"PHPSESSID length:   {len(ps)}")
    print(f"expected username:  {expected}")

    home = "https://funpay.com/"
    lot_edit = (
        "https://funpay.com/lots/offerEdit?node=1316&offer=69300023&location=offer"
    )

    probe("A. golden_key + PHPSESSID, главная",
          {"golden_key": gk, "PHPSESSID": ps}, home, expected)
    probe("B. golden_key + PHPSESSID, lot edit",
          {"golden_key": gk, "PHPSESSID": ps}, lot_edit, expected)
    probe("C. только golden_key, главная",
          {"golden_key": gk}, home, expected)
    probe("D. только golden_key, lot edit",
          {"golden_key": gk}, lot_edit, expected)
    probe("E. только PHPSESSID, главная",
          {"PHPSESSID": ps}, home, expected)

    print("\nЕсли A показывает authenticated=True — сессия валидна и можно идти "
          "дальше с офиц. API; если B при этом ведёт на /account/login — "
          "PHPSESSID инвалидирован по IP/UserAgent. C/D — проверка, можно ли "
          "положиться только на golden_key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
