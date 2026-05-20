from __future__ import annotations

import httpx
import pytest

from src.tools.check_web_api import _get_with_retry


@pytest.mark.asyncio
async def test_get_with_retry_waits_for_temporarily_unavailable_api():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("booting", request=request)
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _get_with_retry(
            client, "/healthz", attempts=3, delay_seconds=0
        )

    assert response.json() == {"status": "ok"}
    assert calls == 3
