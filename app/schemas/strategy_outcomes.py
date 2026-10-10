"""Typed, local Strategy Manager backtest outcome projections."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _OutcomeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class OutcomeMetricsV1(_OutcomeModel):
    total_return: float | None
    sharpe_ratio: float | None
    win_rate: float | None
    max_drawdown: float | None


class OutcomeProvenanceV1(_OutcomeModel):
    quality: Literal["observed_bau", "best_effort_reconstructed", "unavailable"]
    snapshot_count: int = Field(ge=0)


class OutcomeRunV1(_OutcomeModel):
    """Local display row with its verified Result source link."""

    run_id: str = Field(min_length=1)
    evidence_handle: str = Field(pattern=r"^R[0-9]{2}$")
    result_url: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    strategy_api_version: int = Field(ge=1)
    strategy_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    start_month: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}$")
    end_month: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}$")
    base_currency: str = Field(min_length=3, max_length=3)
    starting_capital: str = Field(min_length=1)
    profile_hash: str = Field(min_length=1)
    # Parameter values are already validated against the Strategy protocol
    # when the immutable Result is written; Any avoids Pydantic's implicit
    # recursive-schema failure for the protocol's JsonValue alias.
    parameters: dict[str, Any]
    universe: tuple[str, ...]
    metrics: OutcomeMetricsV1
    metric_display: dict[str, str]
    metric_availability: dict[str, str | None]
    closed_trade_count: int = Field(ge=0)
    equity_point_count: int = Field(ge=0)
    candidate_count: int | None = Field(default=None, ge=0)
    provenance: tuple[OutcomeProvenanceV1, ...]
    is_spy_reference: bool = False


class OutcomeExclusionCountV1(_OutcomeModel):
    reason: str = Field(min_length=1, max_length=80)
    count: int = Field(ge=0)


class OutcomeCohortV1(_OutcomeModel):
    """Local statement of cohort compatibility and known limitations."""

    is_comparable_cohort: bool
    period_start: str | None = None
    period_end: str | None = None
    base_currency: str | None = None
    limitations: tuple[str, ...] = Field(max_length=12)


class StrategyOutcomeSummaryV1(_OutcomeModel):
    """Bounded deterministic landing projection with local source links."""

    state: Literal["cohort", "individual", "empty", "unavailable"]
    candidate_limit: Literal[25] = 25
    inspected_count: int = Field(ge=0, le=25)
    verified_count: int = Field(ge=0, le=25)
    cohort_candidate_count: int = Field(ge=0, le=25)
    cohort_strategy_count: int = Field(ge=0, le=25)
    integrity_excluded_count: int = Field(ge=0, le=25)
    missing_result_count: int = Field(ge=0, le=25)
    job_exclusions: tuple[OutcomeExclusionCountV1, ...] = ()
    comparison_exclusions: tuple[OutcomeExclusionCountV1, ...] = ()
    runs: tuple[OutcomeRunV1, ...] = Field(max_length=25)
    cohort: OutcomeCohortV1
    equity_payload: dict[str, object] | None = None
    equity_mode: Literal["currency", "indexed", "none"] = "none"
    curve_error: str | None = None
    selected_spy_handle: str | None = None
