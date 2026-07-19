"""Shared template context for the canonical watchlist partial."""

from __future__ import annotations

from typing import Any

from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService


def build_watchlist_context(
    trader: TraderService,
    portfolio: PortfolioService,
    **updates: Any,
) -> dict[str, Any]:
    """Build the common context used by every watchlist render path."""
    context: dict[str, Any] = {
        "records": portfolio.load_analysis(),
        "portfolio_tickers": {position.ticker for position in trader.get_portfolio()},
    }
    context.update(updates)
    return context
