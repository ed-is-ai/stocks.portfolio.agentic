"""FastAPI application factory.

Assembles the thin route modules into the app. Business logic lives in the
service layer; routes obtain services via dependency injection.

Run with:
    python -m uvicorn app.api.app:app --reload
"""

from fastapi import FastAPI

from app.api.routes import pipeline, portfolio, trades, views


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(title="Stock Trader")
    app.include_router(views.router)
    app.include_router(portfolio.router)
    app.include_router(trades.router)
    app.include_router(pipeline.router)
    return app


app = create_app()
