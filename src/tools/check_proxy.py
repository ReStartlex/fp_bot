"""
CLI: матричная диагностика прокси.

Зачем: на VPS у нас есть несколько прокси (Telegram-SOCKS5 в .env,
GIT_HTTP_PROXY для git, иногда системный HTTP_PROXY). Когда что-то
ломается — Telegram стучится мимо, git не качает, NS отвечает 403 —
полезно за один прогон увидеть таблицу:

                        direct          telegram        git_http
    ns                  200 OK 0.31s    200 OK 0.71s    -
    funpay              200 OK 0.55s    timeout         -
    telegram-api        200 OK 0.40s    200 OK 0.45s    -
    github              200 OK 0.91s    -               200 OK 0.62s
    gh-proxy            200 OK 0.55s    -               -
    external-ip         1.2.3.4 0.10s   5.6.7.8 0.30s   5.6.7.8 0.25s

Что важно: внешний IP в строке external-ip показывает, через какой
конечный шлюз вышел запрос. Если для Telegram external-ip совпадает
с direct — прокси на самом деле НЕ применился (например, опечатка
в .env). Если все direct'ы зелёные — TELEGRAM_USE_PROXY и
GIT_HTTP_PROXY можно не включать.

Никаких реальных действий (логин, покупки) не делается; всё read-only.

Запуск:
    python -m src.tools.check_proxy
    python -m src.tools.check_proxy --timeout 10 --git-proxy http://user:pass@host:port
    python -m src.tools.check_proxy --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx
from loguru import logger

from src.config import Settings, get_settings


# ─────────────────────────── модель ───────────────────────────


# Имена профилей фиксированы — на них завязаны тесты и summary.
PROFILE_DIRECT = "direct"
PROFILE_TELEGRAM = "telegram"
PROFILE_GIT_HTTP = "git_http"
PROFILE_SYSTEM = "system_env"

# Endpoint'ы, которые мы реально хотим пинговать. Логически делятся
# на группы по тому, какому профилю их имеет смысл проверять.
EP_NS = "ns"
EP_FUNPAY = "funpay"
EP_TELEGRAM = "telegram-api"
EP_TELEGRAM_GETME = "telegram-getme"
EP_GITHUB = "github"
EP_GHPROXY = "gh-proxy"
EP_EXTERNAL_IP = "external-ip"

# Какие endpoints для какого профиля имеют смысл в smart-режиме.
# external-ip всегда — он по сути тест «через какой шлюз я вышел».
SMART_MATRIX: dict[str, frozenset[str]] = {
    PROFILE_DIRECT: frozenset(
        {EP_NS, EP_FUNPAY, EP_TELEGRAM, EP_TELEGRAM_GETME, EP_GITHUB, EP_GHPROXY, EP_EXTERNAL_IP}
    ),
    PROFILE_TELEGRAM: frozenset({EP_TELEGRAM, EP_TELEGRAM_GETME, EP_EXTERNAL_IP}),
    PROFILE_GIT_HTTP: frozenset({EP_GITHUB, EP_GHPROXY, EP_EXTERNAL_IP}),
    PROFILE_SYSTEM: frozenset({EP_NS, EP_FUNPAY, EP_GITHUB, EP_EXTERNAL_IP}),
}


@dataclass(frozen=True)
class ProxyProfile:
    """Один способ ходить наружу: либо прямо (url=None), либо через прокси."""

    name: str
    url: str | None
    source: str  # человекочитаемое «откуда взяли» (например ".env TELEGRAM_PROXY_*")


@dataclass(frozen=True)
class Endpoint:
    """Один URL, который мы дёргаем для проверки доступности."""

    name: str
    url: str
    method: str = "GET"
    # success_codes=None означает «любой ответ HTTP — считаем что endpoint
    # достижим». Это удобно для funpay/github, где даже 403/404 значит «соединение прошло».
    success_codes: frozenset[int] | None = None
    # parse_external_ip=True: для external-ip endpoint берём из body внешний IP.
    parse_external_ip: bool = False


@dataclass
class CheckResult:
    """Результат одного пинга (профиль × endpoint)."""

    profile: str
    endpoint: str
    url: str
    ok: bool
    status: int | None = None
    elapsed_s: float | None = None
    error: str | None = None
    extra: str | None = None  # для external-ip там лежит IP


# ─────────────────────────── чистые функции ───────────────────────────


def mask_proxy_url(url: str | None) -> str | None:
    """Спрятать логин/пароль в URL прокси, оставив user видимым префиксом.

    >>> mask_proxy_url("http://modeler_lLeftL:secret@1.2.3.4:10854")
    'http://mode***:***@1.2.3.4:10854'
    >>> mask_proxy_url("socks5://1.2.3.4:1080")
    'socks5://1.2.3.4:1080'
    >>> mask_proxy_url(None) is None
    True
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid>"
    if not parts.username and not parts.password:
        return url
    user = parts.username or ""
    masked_user = (user[:4] + "***") if user else ""
    masked_pwd = "***" if parts.password else ""
    creds = masked_user
    if masked_pwd:
        creds = f"{creds}:{masked_pwd}" if creds else f":{masked_pwd}"
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if creds:
        netloc = f"{creds}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def discover_profiles(
    settings: Settings,
    *,
    explicit_git_proxy: str | None = None,
    env: dict[str, str] | None = None,
    include_direct: bool = True,
) -> list[ProxyProfile]:
    """Собрать список прокси-профилей для прогонa.

    Telegram-профиль берём из .env даже если TELEGRAM_USE_PROXY=false —
    смысл утилиты в том чтобы дать ответ «работает ли прокси прямо
    сейчас», а не «подключаемся ли мы через него реально». Это
    помогает решить, можно ли его включать.
    """
    env_map = os.environ if env is None else env
    profiles: list[ProxyProfile] = []
    if include_direct:
        profiles.append(ProxyProfile(name=PROFILE_DIRECT, url=None, source="без прокси"))

    tg_url = _build_telegram_proxy_url(settings)
    if tg_url:
        suffix = "" if settings.telegram_use_proxy else " (выключен в .env)"
        profiles.append(
            ProxyProfile(
                name=PROFILE_TELEGRAM,
                url=tg_url,
                source=f".env TELEGRAM_PROXY_*{suffix}",
            )
        )

    git_url = explicit_git_proxy or env_map.get("GIT_HTTP_PROXY")
    if git_url:
        src = "CLI --git-proxy" if explicit_git_proxy else "env GIT_HTTP_PROXY"
        profiles.append(ProxyProfile(name=PROFILE_GIT_HTTP, url=git_url, source=src))

    sys_url = env_map.get("HTTPS_PROXY") or env_map.get("HTTP_PROXY")
    if sys_url and sys_url not in {p.url for p in profiles}:
        profiles.append(
            ProxyProfile(name=PROFILE_SYSTEM, url=sys_url, source="env HTTPS_PROXY/HTTP_PROXY")
        )

    return profiles


def _build_telegram_proxy_url(settings: Settings) -> str | None:
    """То же, что settings.telegram_proxy_url, но игнорирует use_proxy.

    Полезно, чтобы проверить прокси даже если он выключен в .env.
    """
    host = settings.telegram_proxy_host
    port = settings.telegram_proxy_port
    if not host or not port:
        return None
    scheme = settings.telegram_proxy_type.value
    user_obj = settings.telegram_proxy_username
    pwd_obj = settings.telegram_proxy_password
    if user_obj and pwd_obj:
        user = user_obj.get_secret_value()
        pwd = pwd_obj.get_secret_value()
        return f"{scheme}://{user}:{pwd}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def build_endpoints(settings: Settings) -> list[Endpoint]:
    """Собрать список endpoint'ов с учётом доступных секретов.

    NS пингуется без авторизации — нам нужен только TCP+TLS+ответ
    HTTP-сервера, не реальный логин. Тот же подход для FunPay/GitHub.
    """
    endpoints: list[Endpoint] = [
        Endpoint(
            name=EP_NS,
            url=settings.ns_base_url.rstrip("/") + "/",
            # NS без auth отдаёт 401/403 — это всё равно значит «достучались».
            success_codes=None,
        ),
        Endpoint(
            name=EP_FUNPAY,
            url="https://funpay.com/",
            success_codes=None,
        ),
        Endpoint(
            name=EP_TELEGRAM,
            url="https://api.telegram.org/",
            success_codes=None,
        ),
        Endpoint(
            name=EP_GITHUB,
            url="https://github.com/",
            success_codes=None,
        ),
        Endpoint(
            name=EP_GHPROXY,
            url="https://gh-proxy.com/",
            success_codes=None,
        ),
        Endpoint(
            name=EP_EXTERNAL_IP,
            url="https://api.ipify.org?format=text",
            success_codes=frozenset({200}),
            parse_external_ip=True,
        ),
    ]
    token_obj = settings.telegram_bot_token
    if token_obj is not None:
        token = token_obj.get_secret_value()
        # getMe требует валидный токен — это лучшая проверка «бот реально живой».
        endpoints.append(
            Endpoint(
                name=EP_TELEGRAM_GETME,
                url=f"https://api.telegram.org/bot{token}/getMe",
                success_codes=frozenset({200}),
            )
        )
    return endpoints


def _public_url(ep: Endpoint) -> str:
    """URL без секрета (для печати). Маскирует bot-токен."""
    if ep.name == EP_TELEGRAM_GETME:
        return "https://api.telegram.org/bot***/getMe"
    return ep.url


def _should_check(profile_name: str, endpoint_name: str, *, smart: bool) -> bool:
    """В smart-режиме отсекаем заведомо бессмысленные пары (NS через git_http)."""
    if not smart:
        return True
    allowed = SMART_MATRIX.get(profile_name)
    if allowed is None:
        return True
    return endpoint_name in allowed


def summarize(results: Sequence[CheckResult]) -> list[str]:
    """Сжатые рекомендации после прогонa. Чистая функция — на ней висят тесты.

    Логика:
    - если все direct-чеки ok — рекомендуем direct;
    - если direct провалил telegram, но telegram-профиль ok — рекомендуем
      TELEGRAM_USE_PROXY=true;
    - если direct провалил github, но git_http ok — рекомендуем GIT_HTTP_PROXY;
    - если telegram-профиль и direct дают одинаковый external-ip —
      предупреждаем, что прокси не применился.
    """
    by_pe: dict[tuple[str, str], CheckResult] = {(r.profile, r.endpoint): r for r in results}
    profiles = sorted({r.profile for r in results})
    notes: list[str] = []

    def ok(profile: str, ep: str) -> bool | None:
        r = by_pe.get((profile, ep))
        return r.ok if r is not None else None

    if PROFILE_DIRECT in profiles:
        directs = [r for r in results if r.profile == PROFILE_DIRECT and r.endpoint != EP_EXTERNAL_IP]
        if directs and all(r.ok for r in directs):
            notes.append(
                "Прямой доступ ок ко всем endpoint'ам — прокси можно не включать."
            )
        else:
            for ep in (EP_TELEGRAM, EP_TELEGRAM_GETME):
                if ok(PROFILE_DIRECT, ep) is False and ok(PROFILE_TELEGRAM, ep) is True:
                    notes.append(
                        f"Direct до {ep} не работает, но через telegram-прокси — да. "
                        f"Стоит включить TELEGRAM_USE_PROXY=true."
                    )
                    break
            for ep in (EP_GITHUB, EP_GHPROXY):
                if ok(PROFILE_DIRECT, ep) is False and ok(PROFILE_GIT_HTTP, ep) is True:
                    notes.append(
                        f"Direct до {ep} не работает, но через git_http-прокси — да. "
                        f"Задай GIT_HTTP_PROXY перед update.sh."
                    )
                    break

    direct_ip = (by_pe.get((PROFILE_DIRECT, EP_EXTERNAL_IP)) or CheckResult("", "", "", False)).extra
    for other in (PROFILE_TELEGRAM, PROFILE_GIT_HTTP, PROFILE_SYSTEM):
        other_ip = (by_pe.get((other, EP_EXTERNAL_IP)) or CheckResult("", "", "", False)).extra
        if direct_ip and other_ip and direct_ip == other_ip:
            notes.append(
                f"⚠ Профиль '{other}' даёт тот же external-ip что и direct ({direct_ip}). "
                f"Вероятно, прокси НЕ применился (опечатка в URL или SOCKS5 без extras)."
            )

    if not notes:
        notes.append("Ничего критичного не нашёл — смотри сырую матрицу выше.")
    return notes


def render_matrix(results: Sequence[CheckResult]) -> str:
    """Текстовая таблица endpoint × profile. Без зависимостей."""
    profiles = sorted({r.profile for r in results})
    endpoints = sorted({r.endpoint for r in results})
    by_pe: dict[tuple[str, str], CheckResult] = {(r.profile, r.endpoint): r for r in results}

    def cell(r: CheckResult | None) -> str:
        if r is None:
            return "-"
        if not r.ok:
            err = r.error or (f"HTTP {r.status}" if r.status else "fail")
            return f"FAIL {err[:24]}"
        if r.endpoint == EP_EXTERNAL_IP and r.extra:
            return f"{r.extra} ({r.elapsed_s:.2f}s)"
        status = r.status if r.status is not None else "?"
        return f"OK {status} {r.elapsed_s:.2f}s"

    ep_w = max((len(e) for e in endpoints), default=8) + 2
    col_w = 22
    header = "endpoint".ljust(ep_w) + "".join(p.ljust(col_w) for p in profiles)
    lines = [header, "-" * len(header)]
    for ep in endpoints:
        row = ep.ljust(ep_w)
        for p in profiles:
            row += cell(by_pe.get((p, ep))).ljust(col_w)
        lines.append(row)
    return "\n".join(lines)


# ─────────────────────────── сетевая часть ───────────────────────────


async def _probe(
    profile: ProxyProfile, endpoint: Endpoint, *, timeout: float
) -> CheckResult:
    """Один HTTP-запрос. Любая ошибка превращается в CheckResult(ok=False, error=...)."""
    client_kwargs: dict[str, object] = {
        "timeout": timeout,
        "follow_redirects": True,
        "headers": {"User-Agent": "funpay-ns-bot/check_proxy"},
    }
    if profile.url:
        client_kwargs["proxy"] = profile.url
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            r = await client.request(endpoint.method, endpoint.url)
    except httpx.HTTPError as exc:
        return CheckResult(
            profile=profile.name,
            endpoint=endpoint.name,
            url=_public_url(endpoint),
            ok=False,
            elapsed_s=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — diagnostic tool, catch broadly
        return CheckResult(
            profile=profile.name,
            endpoint=endpoint.name,
            url=_public_url(endpoint),
            ok=False,
            elapsed_s=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = time.perf_counter() - started
    ok = (
        endpoint.success_codes is None
        or r.status_code in endpoint.success_codes
    )
    extra: str | None = None
    if endpoint.parse_external_ip and ok:
        ip = r.text.strip()
        if 7 <= len(ip) <= 45:
            extra = ip
    return CheckResult(
        profile=profile.name,
        endpoint=endpoint.name,
        url=_public_url(endpoint),
        ok=ok,
        status=r.status_code,
        elapsed_s=elapsed,
        extra=extra,
    )


async def run_checks(
    profiles: Iterable[ProxyProfile],
    endpoints: Iterable[Endpoint],
    *,
    timeout: float = 8.0,
    smart: bool = True,
) -> list[CheckResult]:
    """Прогнать (profile × endpoint) — все пары конкурентно, smart-фильтр опционален."""
    tasks: list[asyncio.Task[CheckResult]] = []
    for profile in profiles:
        for ep in endpoints:
            if not _should_check(profile.name, ep.name, smart=smart):
                continue
            tasks.append(asyncio.create_task(_probe(profile, ep, timeout=timeout)))
    if not tasks:
        return []
    return list(await asyncio.gather(*tasks))


# ─────────────────────────── CLI ───────────────────────────


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_proxy",
        description="Матричная проверка доступности NS/FunPay/Telegram/GitHub через разные прокси.",
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="Таймаут одного запроса, сек.")
    parser.add_argument(
        "--git-proxy",
        default=None,
        help="Дополнительный HTTP-прокси для профиля git_http (перекрывает GIT_HTTP_PROXY).",
    )
    parser.add_argument(
        "--no-direct",
        action="store_true",
        help="Не запускать профиль 'direct' (без прокси).",
    )
    parser.add_argument(
        "--full-matrix",
        action="store_true",
        help="Прогнать ВСЕ пары, без smart-фильтра (например ns через git_http).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывод в JSON (для скриптов/CI).",
    )
    return parser.parse_args(argv)


async def _amain(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    profiles = discover_profiles(
        settings,
        explicit_git_proxy=args.git_proxy,
        include_direct=not args.no_direct,
    )
    endpoints = build_endpoints(settings)

    if not profiles:
        logger.error("Не нашёл ни одного профиля для проверки (включая direct).")
        return 1

    if not args.json:
        logger.info("=" * 70)
        logger.info("Профили:")
        for p in profiles:
            logger.info(f"  - {p.name:10s} {mask_proxy_url(p.url) or '(direct)':40s} [{p.source}]")
        logger.info("Endpoints:")
        for ep in endpoints:
            logger.info(f"  - {ep.name:15s} {_public_url(ep)}")
        logger.info("=" * 70)

    results = await run_checks(profiles, endpoints, timeout=args.timeout, smart=not args.full_matrix)

    if args.json:
        payload = {
            "profiles": [
                {**asdict(p), "url": mask_proxy_url(p.url)} for p in profiles
            ],
            "results": [asdict(r) for r in results],
            "notes": summarize(results),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        logger.info("\n" + render_matrix(results))
        logger.info("=" * 70)
        for note in summarize(results):
            logger.info(note)
        logger.info("=" * 70)

    failures = sum(1 for r in results if not r.ok and r.profile == PROFILE_DIRECT)
    return 0 if failures == 0 else 2


def main(argv: Sequence[str] | None = None) -> int:
    from src.logging_setup import setup_logging

    setup_logging()
    try:
        return asyncio.run(_amain(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
