"""Shared template context for the canonical watchlist partial."""

from __future__ import annotations

from typing import Any

from app.core.alerting import AlertUiState, build_alert_ui_state
from app.core.config import ANALYSIS_JSON, PIPELINE_STATUS_JSON
from app.core.recommendation import (
    Recommendation,
    actionability_sort_key,
    classify_recommendation,
)
from app.repositories.alerts_repo import AlertsRepository
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.analysis_artifact import read_analysis_artifact_meta
from app.schemas.pipeline_status import PipelineState, PipelineStatus
from app.schemas.source_health import SourceHealth, SourceName
from app.services.freshness_service import Freshness, calculate_freshness
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

_status_repository = PipelineStatusRepository(PIPELINE_STATUS_JSON)


def _load_current_owner() -> PipelineStatus | None:
    """Return the run-status record owning the artifact currently on disk.

    Freshness *timing* comes from the artifact's own embedded metadata (see
    ``app.schemas.analysis_artifact``) — that is the single fact that is
    always consistent with what ``portfolio.load_analysis()`` renders, even
    if the process crashed before this run's terminal status was recorded.
    This lookup only enriches that with source-health coverage for the same
    run, on a best-effort basis.
    """
    meta = read_analysis_artifact_meta(ANALYSIS_JSON)
    if meta is None:
        return None
    return _status_repository.find_run(meta.run_id)


def load_source_health() -> dict[SourceName, SourceHealth]:
    """Load coverage for the run that owns the currently displayed analysis."""
    owner = _load_current_owner()
    return owner.source_health if owner else {}


def build_freshness_context() -> dict[str, Any]:
    """Build freshness metadata independently from the latest attempt state."""
    meta = read_analysis_artifact_meta(ANALYSIS_JSON)
    freshness: Freshness = calculate_freshness(meta.generated_at if meta else None)
    latest = _status_repository.load()
    return {
        "freshness": freshness,
        "latest_attempt_error": (
            latest.error_summary if latest.state is PipelineState.FAILED else None
        ),
    }


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

    Freshness and source-health context live on the bottom status bar
    (`/pipeline-status`, see `app.api.routes.pipeline`) instead of here (#71).
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
