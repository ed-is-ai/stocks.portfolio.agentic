"""FastAPI application factory.

Assembles the thin route modules into the app. Business logic lives in the
service layer; routes obtain services via dependency injection.

Run with:
    python -m uvicorn app.api.app:app --reload
"""

from contextlib import asynccontextmanager
import sqlite3
from typing import AsyncIterator, Protocol

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
    notifications,
    pipeline,
    portfolio,
    portfolios,
    settings,
    strategy_manager,
    trades,
    views,
)
from app.core.config import STATIC_DIR
from app.core import config
from app.api.dependencies import (
    get_strategy_job_service,
    get_strategy_notification_projector,
)


class StrategyJobLifecycleService(Protocol):
    def reconcile_startup(self) -> object: ...

    def start_dispatcher(self) -> None: ...

    def shutdown(self) -> None: ...


def create_app(
    *,
    strategy_job_service: StrategyJobLifecycleService | None = None,
    strategy_jobs_enabled: bool | None = None,
) -> FastAPI:
    """Build and return the FastAPI application."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        enabled = (
            config.strategy_manager_worker_enabled()
            if strategy_jobs_enabled is None
            else strategy_jobs_enabled
        )
        service = None
        try:
            get_strategy_notification_projector().project_pending()
        except sqlite3.OperationalError:
            # The notification centre remains available if the optional
            # Strategy Manager store is not configured or temporarily offline.
            pass
        if enabled:
            service = strategy_job_service or get_strategy_job_service()
            service.reconcile_startup()
        try:
            get_strategy_notification_projector().project_pending()
        except sqlite3.OperationalError:
            # The notification centre remains available if the optional
            # Strategy Manager store is not configured or temporarily offline.
            pass
        if service is not None:
            service.start_dispatcher()
        try:
            yield
        finally:
            if service is not None:
                service.shutdown()

    app = FastAPI(title="Stock Trader", lifespan=lifespan)
    app.include_router(views.router)
    app.include_router(portfolios.router)
    app.include_router(portfolio.router)
    app.include_router(trades.router)
    app.include_router(pipeline.router)
    app.include_router(notifications.router)
    app.include_router(settings.router)
    app.include_router(strategy_manager.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
