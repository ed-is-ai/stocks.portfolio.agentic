"""Shared template context for the canonical watchlist partial."""

from __future__ import annotations

from typing import Any

from app.core.alerting import AlertUiState, build_alert_ui_state
from app.core.recommendation import (
    Recommendation,
    actionability_sort_key,
    classify_recommendation,
)
from app.repositories.alerts_repo import AlertsRepository
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService


def build_watchlist_context(
    trader: TraderService,
    portfolio: PortfolioService,
    alerts: AlertsRepository,
    **updates: Any,
) -> dict[str, Any]:
    """Build the common context used by every watchlist render path.

    Records are sorted actionable-first (then by score) so the most tradable
    setups surface at the top regardless of the order persisted on disk.

    Also precomputes, per ticker, the canonical recommendation bucket and
    the alert cooldown/suppression state (#58) so the template only renders
    fields rather than re-deriving the underlying thresholds in Jinja.
    """
    records = sorted(
        portfolio.load_analysis(), key=actionability_sort_key, reverse=True
    )
    portfolio_tickers = {position.ticker for position in trader.get_portfolio()}

    recommendations: dict[str, Recommendation] = {}
    alert_states: dict[str, AlertUiState] = {}
    for record in records:
        recommendations[record.ticker] = classify_recommendation(
            record, is_portfolio_holding=record.ticker in portfolio_tickers
        )
        alert_states[record.ticker] = build_alert_ui_state(
            record,
            has_watching=alerts.has_watching(record.ticker),
            last_alerted_at=alerts.last_alerted_at(record.ticker),
        )

    context: dict[str, Any] = {
        "records": records,
        "portfolio_tickers": portfolio_tickers,
        "recommendations": recommendations,
        "alert_states": alert_states,
    }
    context.update(updates)
    return context
