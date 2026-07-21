"""Shared template context for the canonical watchlist partial."""

from __future__ import annotations

from typing import Any

from app.core.recommendation import actionability_sort_key
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService


def build_watchlist_context(
    trader: TraderService,
    portfolio: PortfolioService,
    **updates: Any,
) -> dict[str, Any]:
    """Build the common context used by every watchlist render path.

    Records are sorted actionable-first (then by score) so the most tradable
    setups surface at the top regardless of the order persisted on disk.
    """
    records = sorted(
        portfolio.load_analysis(), key=actionability_sort_key, reverse=True
    )
    context: dict[str, Any] = {
        "records": records,
        "portfolio_tickers": {position.ticker for position in trader.get_portfolio()},
    }
    context.update(updates)
    return context
