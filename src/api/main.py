"""CLI entrypoint для запуска Web API отдельно от основного бота."""
from __future__ import annotations

import uvicorn

from src.config import get_settings


def main() -> int:
    settings = get_settings()
    uvicorn.run(
        "src.api.app:app",
        host=settings.web_api_host,
        port=settings.web_api_port,
        reload=False,
        factory=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
