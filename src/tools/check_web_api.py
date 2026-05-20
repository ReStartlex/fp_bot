"""Smoke-проверка локального Web API."""
from __future__ import annotations

import asyncio
import sys

import httpx

from src.config import get_settings


async def _get_with_retry(
    client: httpx.AsyncClient,
    path: str,
    *,
    attempts: int = 12,
    delay_seconds: float = 1.0,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.get(path, headers=headers)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                await asyncio.sleep(delay_seconds)
    assert last_exc is not None
    raise last_exc


async def _main() -> int:
    settings = get_settings()
    base_url = f"http://{settings.web_api_host}:{settings.web_api_port}"
    timeout = httpx.Timeout(5.0)

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        try:
            health = await _get_with_retry(client, "/healthz")
        except Exception as exc:
            print(f"❌ /healthz недоступен на {base_url}: {exc}")
            return 1
        print(f"✅ /healthz: {health.json()}")

        token = (
            settings.web_api_token.get_secret_value()
            if settings.web_api_token is not None
            else None
        )
        if not token:
            print("⚠ WEB_API_TOKEN не задан — защищённые endpoints не проверяю.")
            return 0

        try:
            dashboard = await _get_with_retry(
                client,
                "/api/dashboard",
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            print(f"❌ /api/dashboard недоступен или auth не прошёл: {exc}")
            return 1
        data = dashboard.json()
        orders = data.get("orders", {})
        mappings = data.get("mappings", {})
        print(
            "✅ /api/dashboard: "
            f"orders={orders.get('total', '?')}, "
            f"mappings={mappings.get('total', '?')}"
        )
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
