"""Shared builders for the Strategy stop-suggestion tests.

Imported by the stop suggestion, set-stop route and agents route tests, so
no test module imports helpers from another.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.schemas.portfolio_recommendation import (
    RecommendationResultV1,
    RecommendationStopLevelV1,
)
from app.schemas.strategy_assignment import ScanFreshness


def stop_level(level: str | None = "95", **overrides: Any) -> RecommendationStopLevelV1:
    """A market-basis Strategy stop level in GBP, as the service projects it."""
    fields: dict[str, Any] = {
        "level": None if level is None else Decimal(level),
        "rule_code": "close_below_sma50",
        "summary": "Close below the 50-day average",
        "facts": ("Close that breaks the 50-session SMA: 95",),
        "currency": "GBP",
        "basis": "market",
        "trigger": "close_lt",
    }
    return RecommendationStopLevelV1(**(fields | overrides))


def result(
    freshness: ScanFreshness = "fresh", **stop_levels: RecommendationStopLevelV1
) -> RecommendationResultV1:
    """A recommendation result carrying ``stop_levels`` keyed by security id."""
    now = datetime(2026, 9, 28, tzinfo=UTC)
    return RecommendationResultV1(
        portfolio_id=7,
        analysis_run_id="run-1",
        generated_at=now,
        market_session=date(2026, 9, 25),
        freshness=freshness,
        strategy_id="rtly-backtest-minervini",
        strategy_source_digest="a" * 64,
        parameters={},
        recommendations=(),
        evaluated_at=now,
        stop_levels=stop_levels,
    )
