"""Strict allowlists and output contracts for Strategy Manager model calls."""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

InsightScalar = StrictStr | StrictInt | StrictFloat | StrictBool | None


class _InsightModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class StrategyInsightParameterV1(_InsightModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    value: InsightScalar

    @field_validator("value")
    @classmethod
    def _bounded_string(cls, value: InsightScalar) -> InsightScalar:
        if isinstance(value, str) and len(value) > 256:
            raise ValueError("parameter text value is too long")
        return value


class StrategyInsightMetricsV1(_InsightModel):
    total_return: float | None
    sharpe_ratio: float | None
    win_rate: float | None
    max_drawdown: float | None


class StrategyInsightProvenanceV1(_InsightModel):
    quality: Literal["observed_bau", "best_effort_reconstructed", "unavailable"]
    snapshot_count: int = Field(ge=0)


class StrategyInsightRunV1(_InsightModel):
    evidence_handle: str = Field(pattern=r"^R[0-9]{2}$")
    strategy_id: str = Field(
        min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    strategy_api_version: int = Field(ge=1)
    start_month: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}$")
    end_month: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    parameters: tuple[StrategyInsightParameterV1, ...] = Field(max_length=64)
    universe_tickers: tuple[str, ...] = Field(max_length=1000)
    universe_symbol_count: int = Field(ge=0)
    universe_symbols_truncated: bool
    metrics: StrategyInsightMetricsV1
    metric_availability: dict[
        Literal["total_return", "sharpe_ratio", "win_rate", "max_drawdown"],
        str | None,
    ]
    closed_trade_count: int = Field(ge=0)
    candidate_count: int | None = Field(default=None, ge=0)
    provenance: tuple[StrategyInsightProvenanceV1, ...] = Field(max_length=12)
    is_pinned_spy_reference: bool

    @field_validator("universe_tickers")
    @classmethod
    def _valid_tickers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(value) > 32
            or not value
            or not value[0].isalnum()
            or any(not (char.isalnum() or char in ".-") for char in value)
            for value in values
        ):
            raise ValueError("universe contains an invalid ticker symbol")
        return values


class StrategyInsightExclusionV1(_InsightModel):
    reason: str = Field(min_length=1, max_length=80)
    count: int = Field(ge=0)


class StrategyInsightSummaryV1(_InsightModel):
    version: Literal["strategy-outcomes.v1"] = "strategy-outcomes.v1"
    summary_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["cohort", "individual", "empty", "unavailable"]
    period_start: str | None = None
    period_end: str | None = None
    currency: str | None = None
    inspected_count: int = Field(ge=0, le=25)
    verified_count: int = Field(ge=0, le=25)
    cohort_candidate_count: int = Field(ge=0, le=25)
    cohort_strategy_count: int = Field(ge=0, le=25)
    integrity_excluded_count: int = Field(ge=0, le=25)
    missing_result_count: int = Field(ge=0, le=25)
    job_exclusions: tuple[StrategyInsightExclusionV1, ...] = Field(max_length=25)
    comparison_exclusions: tuple[StrategyInsightExclusionV1, ...] = Field(max_length=25)
    limitations: tuple[str, ...] = Field(max_length=12)
    runs: tuple[StrategyInsightRunV1, ...] = Field(max_length=25)


class StrategyQuestionContextV1(_InsightModel):
    strategy_id: str = Field(
        min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    strategy_api_version: int = Field(ge=1)
    declared_parameters: tuple[StrategyInsightParameterV1, ...] = Field(max_length=64)


class StrategyInsightsRequestV1(_InsightModel):
    summary: StrategyInsightSummaryV1


class StrategyQuestionRequestV1(_InsightModel):
    summary: StrategyInsightSummaryV1
    question: str = Field(min_length=1, max_length=500)
    current_strategy: StrategyQuestionContextV1


class StrategyInsightClaimV1(_InsightModel):
    text: str = Field(min_length=1, max_length=600)
    evidence_handles: tuple[str, ...] = Field(min_length=1, max_length=25)
    strategy_ids: tuple[str, ...] = Field(max_length=25)


class StrategyInsightIdeaV1(_InsightModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=600)
    evidence_handles: tuple[str, ...] = Field(min_length=1, max_length=25)


class StrategyInsightsReportV1(_InsightModel):
    observations: tuple[StrategyInsightClaimV1, ...] = Field(max_length=8)
    hypotheses: tuple[StrategyInsightClaimV1, ...] = Field(max_length=8)
    strategies_to_explore: tuple[StrategyInsightIdeaV1, ...] = Field(max_length=5)

    @model_validator(mode="after")
    def _has_content(self) -> "StrategyInsightsReportV1":
        if not (self.observations or self.hypotheses or self.strategies_to_explore):
            raise ValueError("insight report must contain at least one cited item")
        return self


class StrategyQuestionAnswerV1(_InsightModel):
    answer: str = Field(min_length=1, max_length=2000)
    citations: tuple[str, ...] = Field(max_length=25)
    unknowns: tuple[str, ...] = Field(max_length=12)


class StrategyAgentAttemptV1(_InsightModel):
    model_provider: Literal["anthropic", "foundry_local"]
    model_id: str = Field(min_length=1, max_length=120)
    outcome: Literal["selected", "no_valid_output", "unavailable", "not_configured"]
