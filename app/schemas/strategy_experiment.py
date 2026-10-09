"""Typed contracts for auditable, one-parameter Strategy experiments."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.backtest.strategy_protocol import JsonScalar


class _ExperimentModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class ExperimentMetric(StrEnum):
    TOTAL_RETURN = "total_return"
    SHARPE_RATIO = "sharpe_ratio"
    WIN_RATE = "win_rate"
    MAX_DRAWDOWN = "max_drawdown"


class ExpectedDirection(StrEnum):
    HIGHER = "higher"
    LOWER = "lower"


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    DISCARDED = "discarded"
    COMPLETE = "complete"
    INCONCLUSIVE = "inconclusive"


class ExperimentVerdict(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"


class StrategyExperimentProposalV1(_ExperimentModel):
    """Untrusted model proposal; it contains no run identity or enqueue data."""

    parameter_name: Annotated[str, Field(min_length=1, max_length=120)]
    proposed_value: JsonScalar
    effect_summary: Annotated[str, Field(min_length=1, max_length=1000)]
    metric: ExperimentMetric
    expected_direction: ExpectedDirection


class StrategyExperimentModelAttemptV1(_ExperimentModel):
    """One proposal provider attempted before the saved draft was created."""

    model_provider: Literal["anthropic", "foundry_local"]
    model_id: Annotated[str, Field(min_length=1, max_length=120)]
    outcome: Literal["selected", "no_valid_proposal"]


class StrategyExperimentDraftV1(_ExperimentModel):
    schema_version: Literal["strategy_experiment_draft.v1"] = (
        "strategy_experiment_draft.v1"
    )
    baseline_run_id: Annotated[str, Field(min_length=1)]
    hypothesis: Annotated[str, Field(min_length=1, max_length=2000)]
    strategy_id: Annotated[str, Field(min_length=1)]
    strategy_api_version: Annotated[int, Field(ge=1)]
    strategy_source_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    parameter_name: Annotated[str, Field(min_length=1, max_length=120)]
    baseline_value: JsonScalar
    proposed_value: JsonScalar
    effect_summary: Annotated[str, Field(min_length=1, max_length=1000)]
    metric: ExperimentMetric
    expected_direction: ExpectedDirection
    baseline_manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    baseline_manifest_json: Annotated[str, Field(min_length=2)]
    model_provider: Literal["anthropic", "foundry_local"] = "foundry_local"
    model_id: Annotated[str, Field(min_length=1, max_length=120)]
    model_attempts: tuple[StrategyExperimentModelAttemptV1, ...] = ()
    created_at: datetime


class StrategyExperimentApprovalV1(_ExperimentModel):
    approved_at: datetime
    draft_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    actor: Literal["local_user", "api_token"] = "local_user"


class StrategyExperimentComparisonV1(_ExperimentModel):
    baseline_run_id: Annotated[str, Field(min_length=1)]
    candidate_run_id: Annotated[str, Field(min_length=1)]
    metric: ExperimentMetric
    baseline_value: float | None
    candidate_value: float | None
    baseline_closed_trades: int | None = Field(default=None, ge=0)
    candidate_closed_trades: int | None = Field(default=None, ge=0)
    eligibility_reason: str | None = None
    baseline_manifest_digest: str | None = None
    candidate_manifest_digest: str | None = None
    execution_contract_digest: str | None = None
    limitations: tuple[str, ...] = ()


class StrategyExperimentConclusionV1(_ExperimentModel):
    verdict: ExperimentVerdict
    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    concluded_at: datetime
    comparison: StrategyExperimentComparisonV1


class StrategyExperimentV1(_ExperimentModel):
    id: Annotated[str, Field(min_length=1)]
    status: ExperimentStatus
    draft: StrategyExperimentDraftV1
    draft_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    candidate_run_id: str | None = None
    approval: StrategyExperimentApprovalV1 | None = None
    comparison: StrategyExperimentComparisonV1 | None = None
    conclusion: StrategyExperimentConclusionV1 | None = None
    created_at: datetime
    updated_at: datetime


class StrategyExperimentAuditEventV1(_ExperimentModel):
    sequence: int = Field(gt=0)
    experiment_id: str | None = None
    baseline_run_id: str | None = None
    candidate_run_id: str | None = None
    event_type: Annotated[str, Field(min_length=1, max_length=80)]
    occurred_at: datetime
    details: dict[str, object]


class StrategyExperimentDraftOutcomeV1(_ExperimentModel):
    status: Literal["created", "unavailable", "rejected"]
    experiment: StrategyExperimentV1 | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class StrategyExperimentBaselineOptionV1(_ExperimentModel):
    id: Annotated[str, Field(min_length=1)]
    strategy_id: Annotated[str, Field(min_length=1)]
    start_month: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]
    end_month: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]


class StrategyExperimentDetailV1(_ExperimentModel):
    experiment: StrategyExperimentV1
    locked_manifest_json: str
    candidate_status: str | None = None
    audit_events: tuple[StrategyExperimentAuditEventV1, ...] = ()
