"""FastAPI backend: тонкий HTTP-слой поверх application services."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query

from src.api.auth import require_api_auth
from src.config import Settings, get_settings
from src.db.session import close_db, init_db
from src.logging_setup import setup_logging
from src.orders.reconciler import reconcile_orders_once
from src.services.admin import (
    get_dashboard_summary,
    get_profit_summary,
    list_mappings,
    list_orders,
    list_problem_items,
)
from src.sync.stock_sync import sync_once


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings)
    await init_db()
    try:
        yield
    finally:
        await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="FunPay NS Bot Admin API",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/dashboard", dependencies=[Depends(require_api_auth)], tags=["admin"])
    async def dashboard(settings: Settings = Depends(get_settings)) -> dict:
        return await get_dashboard_summary(settings)

    @app.get("/api/orders", dependencies=[Depends(require_api_auth)], tags=["admin"])
    async def orders(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        status: str | None = None,
    ) -> list[dict]:
        return await list_orders(limit=limit, status=status)

    @app.get("/api/mappings", dependencies=[Depends(require_api_auth)], tags=["admin"])
    async def mappings(
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        enabled: bool | None = None,
    ) -> list[dict]:
        return await list_mappings(limit=limit, enabled=enabled)

    @app.get("/api/problems", dependencies=[Depends(require_api_auth)], tags=["admin"])
    async def problems(limit: Annotated[int, Query(ge=1, le=200)] = 100) -> dict:
        return await list_problem_items(limit=limit)

    @app.get("/api/profit", dependencies=[Depends(require_api_auth)], tags=["admin"])
    async def profit(
        days: Annotated[int, Query(ge=1, le=90)] = 7,
        settings: Settings = Depends(get_settings),
    ) -> dict:
        return await get_profit_summary(days=days, settings=settings)

    @app.post("/api/sync", dependencies=[Depends(require_api_auth)], tags=["ops"])
    async def run_sync(dry_run: bool = True) -> dict:
        return await sync_once(dry_run=dry_run)

    @app.post("/api/reconcile", dependencies=[Depends(require_api_auth)], tags=["ops"])
    async def run_reconcile(settings: Settings = Depends(get_settings)) -> dict:
        return await reconcile_orders_once(settings=settings)

    return app


app = create_app()
