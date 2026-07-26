"""FastAPI application factory.

Assembles the thin route modules into the app. Business logic lives in the
service layer; routes obtain services via dependency injection.

Run with:
    python -m uvicorn app.api.app:app --reload
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
    notifications,
    pipeline,
    portfolio,
    settings,
    trades,
    views,
)
from app.core.config import STATIC_DIR


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(title="Stock Trader")
    app.include_router(views.router)
    app.include_router(portfolio.router)
    app.include_router(trades.router)
    app.include_router(pipeline.router)
    app.include_router(notifications.router)
    app.include_router(settings.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
