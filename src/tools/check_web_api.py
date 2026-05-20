"""Smoke-проверка локального Web API."""
from __future__ import annotations

import asyncio
import sys

import httpx

from src.config import get_settings


async def _main() -> int:
    settings = get_settings()
    base_url = f"http://{settings.web_api_host}:{settings.web_api_port}"
    timeout = httpx.Timeout(5.0)

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        try:
            health = await client.get("/healthz")
            health.raise_for_status()
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
            dashboard = await client.get(
                "/api/dashboard",
                headers={"Authorization": f"Bearer {token}"},
            )
            dashboard.raise_for_status()
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
