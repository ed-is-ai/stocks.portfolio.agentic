"""Typed durable lifecycle contracts for Strategy Manager work."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_scan_record import FrozenDict
from app.services.backtest.trading_calendar import TradingCalendar


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class StrategyJobType(StrEnum):
    INITIALIZATION = "initialization"
    BACKTEST = "backtest"


class StrategyJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETE, self.FAILED, self.CANCELLED}


class JobFailureCode(StrEnum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_THROTTLED = "provider_throttled"
    PROVIDER_CONTRACT_ERROR = "provider_contract_error"
    REQUIRED_DATA_MISSING = "required_data_missing"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    CALENDAR_ERROR = "calendar_error"
    INTEGRITY_ERROR = "integrity_error"
    WORKER_INTERRUPTED = "worker_interrupted"


class StrategyJobError(RuntimeError):
    """Base typed lifecycle error with a stable machine code."""

    code = "strategy_job_error"


class StrategyJobConflict(StrategyJobError):
    code = "job_conflict"


class StrategyJobNotFound(StrategyJobError):
    code = "job_not_found"


class _LifecycleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StrategyJobV1(_LifecycleModel):
    id: Annotated[str, Field(min_length=1)]
    job_type: StrategyJobType
    status: StrategyJobStatus
    parent_job_id: str | None = None
    enqueue_seq: Annotated[int, Field(gt=0)]
    claim_token: str | None = None
    current_month: Month | None = None
    status_version: Annotated[int, Field(gt=0)]
    cancel_requested_at: datetime | None = None
    failure_code: JobFailureCode | None = None
    failed_month: Month | None = None
    failure_detail: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    deleted_at: datetime | None = None
    audit_summary: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _valid_state_shape(self) -> "StrategyJobV1":
        for value in (
            self.cancel_requested_at,
            self.deleted_at,
            self.created_at,
            self.updated_at,
        ):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("job timestamps must be timezone-aware")
        if self.status is StrategyJobStatus.QUEUED and (
            self.claim_token is not None or self.current_month is not None
        ):
            raise ValueError("queued jobs cannot carry worker ownership or progress")
        if self.status is StrategyJobStatus.RUNNING and self.claim_token is None:
            raise ValueError("running jobs require a claim token")
        if self.status.terminal and self.current_month is not None:
            raise ValueError("terminal jobs cannot carry current progress")
        failed = self.status is StrategyJobStatus.FAILED
        if failed != (
            self.failure_code is not None and self.failure_detail is not None
        ):
            raise ValueError("failure fields must match failed status")
        if not failed and self.failed_month is not None:
            raise ValueError("only failed jobs may carry a failed month")
        return self


class InitializationRunV1(_LifecycleModel):
    job_id: Annotated[str, Field(min_length=1)]
    profile_hash: Digest
    requested_start: Month
    requested_end: Month
    requested_months: tuple[Month, ...]
    requested_month_digest: Digest
    calendar_dataset_version: Annotated[str, Field(min_length=1)]
    qualification_contract_digest: Digest
    ordered_month_digest: Digest | None = None

    @model_validator(mode="after")
    def _valid_range(self) -> "InitializationRunV1":
        expected = TradingCalendar.months_inclusive(
            self.requested_start, self.requested_end
        )
        if self.requested_months != expected:
            raise ValueError("initialization requested months do not match its range")
        digest = manifest_digest(
            {
                "schema_version": "initialization_requested_months.v1",
                "profile_hash": self.profile_hash,
                "months": expected,
                "calendar_dataset_version": self.calendar_dataset_version,
            }
        )
        if self.requested_month_digest != digest:
            raise ValueError("initialization requested-month digest is invalid")
        return self


class BacktestRunV1(_LifecycleModel):
    """One pinned Backtest Run's immutable ``strategy_runs`` identity
    (AD-9): the exact Strategy/version/source, validated parameters,
    pinned profile/period/evidence digest, capital/currency, and the
    content-addressed Run-input-manifest binding a claimed worker replays
    from -- mirrors ``InitializationRunV1``'s strict frozen shape."""

    job_id: Annotated[str, Field(min_length=1)]
    strategy_id: Annotated[str, Field(min_length=1)]
    strategy_api_version: Annotated[int, Field(ge=1)]
    strategy_source_digest: Digest
    parameters: dict[str, object]
    profile_hash: Digest
    start_month: Month
    end_month: Month
    ordered_month_digest: Digest
    base_currency: Literal["GBP", "USD"]
    starting_capital: Decimal
    run_input_manifest_digest: Digest
    execution_contract_digest: Digest

    @field_validator("parameters")
    @classmethod
    def _immutable_parameters(cls, value: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], FrozenDict(value))

    @model_validator(mode="after")
    def _valid_run(self) -> "BacktestRunV1":
        if self.start_month > self.end_month:
            raise ValueError("backtest start_month must not be after end_month")
        if not self.starting_capital.is_finite() or self.starting_capital <= 0:
            raise ValueError("backtest starting_capital must be positive and finite")
        return self


class ClaimedStrategyJobV1(_LifecycleModel):
    job: StrategyJobV1
    initialization: InitializationRunV1 | None = None
    backtest: BacktestRunV1 | None = None
    claim_token: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _matches_job(self) -> "ClaimedStrategyJobV1":
        if (
            self.job.status is not StrategyJobStatus.RUNNING
            or self.job.claim_token != self.claim_token
        ):
            raise ValueError("claim does not own the running job")
        if self.job.job_type is StrategyJobType.INITIALIZATION:
            if self.initialization is None or self.initialization.job_id != self.job.id:
                raise ValueError("initialization claim requires its subtype")
            if self.backtest is not None:
                raise ValueError(
                    "initialization claim must not carry a backtest subtype"
                )
        elif self.job.job_type is StrategyJobType.BACKTEST:
            if self.backtest is None or self.backtest.job_id != self.job.id:
                raise ValueError("backtest claim requires its subtype")
            if self.initialization is not None:
                raise ValueError(
                    "backtest claim must not carry an initialization subtype"
                )
        return self


class InitializationEnqueueResultV1(_LifecycleModel):
    no_op: bool
    job: StrategyJobV1 | None = None
    initialization: InitializationRunV1 | None = None

    @model_validator(mode="after")
    def _consistent_result(self) -> "InitializationEnqueueResultV1":
        if self.no_op:
            if self.job is not None or self.initialization is not None:
                raise ValueError("no-op initialization cannot contain a job")
        elif (
            self.job is None
            or self.initialization is None
            or self.initialization.job_id != self.job.id
        ):
            raise ValueError("queued initialization requires matching job and subtype")
        return self


class BacktestEnqueueResultV1(_LifecycleModel):
    """One durably enqueued (or idempotently returned) Backtest attempt --
    always carries both the job and its matching Strategy Run, unlike
    ``InitializationEnqueueResultV1``'s ``no_op`` shape: a Backtest
    submission always creates or returns a real attempt, never a no-op."""

    job: StrategyJobV1
    backtest: BacktestRunV1

    @model_validator(mode="after")
    def _consistent_result(self) -> "BacktestEnqueueResultV1":
        if self.backtest.job_id != self.job.id:
            raise ValueError("backtest run does not match its job")
        return self


class BacktestSubmissionV1(_LifecycleModel):
    """One caller-validated Backtest launch request (Story 2.6 AC 1).

    Carries the exact immutable identity ``create_backtest_job`` persists
    into ``strategy_runs`` plus the content-addressed Run-input-manifest
    binding a caller (typically a launch flow built atop Story 2.3's
    ``build_run_input_manifest``) already resolved and canonically
    rendered. Deliberately excludes ``ordered_month_digest`` -- the
    repository always revalidates and recomputes it fresh at enqueue time
    rather than trusting a caller-supplied value that may have gone stale
    between manifest build and submission.
    """

    strategy_id: Annotated[str, Field(min_length=1)]
    strategy_api_version: Annotated[int, Field(ge=1)]
    strategy_source_digest: Digest
    parameters: dict[str, object]
    profile_hash: Digest
    start_month: Month
    end_month: Month
    base_currency: Literal["GBP", "USD"]
    starting_capital: Decimal
    run_input_manifest_digest: Digest
    execution_contract_digest: Digest
    canonical_manifest_json: Annotated[str, Field(min_length=1)]
    parent_job_id: str | None = None
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @field_validator("parameters")
    @classmethod
    def _immutable_parameters(cls, value: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], FrozenDict(value))

    @model_validator(mode="after")
    def _valid_range(self) -> "BacktestSubmissionV1":
        TradingCalendar.months_inclusive(self.start_month, self.end_month)
        if not self.starting_capital.is_finite() or self.starting_capital <= 0:
            raise ValueError("backtest starting_capital must be positive and finite")
        return self


class InitializationSubmissionV1(_LifecycleModel):
    profile_hash: Digest
    requested_start: Month
    requested_end: Month
    calendar_dataset_version: Annotated[str, Field(min_length=1)]
    parent_job_id: str | None = None

    @model_validator(mode="after")
    def _closed_range(self) -> "InitializationSubmissionV1":
        TradingCalendar.months_inclusive(self.requested_start, self.requested_end)
        return self


class StrategyJobCancellationV1(_LifecycleModel):
    job_id: Annotated[str, Field(min_length=1)]
    expected_version: Annotated[int, Field(gt=0)]


class StrategyJobProgressV1(_LifecycleModel):
    job_id: Annotated[str, Field(min_length=1)]
    claim_token: Annotated[str, Field(min_length=1)]
    expected_version: Annotated[int, Field(gt=0)]
    month: Month


class StrategyJobFailureV1(_LifecycleModel):
    job_id: Annotated[str, Field(min_length=1)]
    claim_token: Annotated[str, Field(min_length=1)]
    expected_version: Annotated[int, Field(gt=0)]
    failure_code: JobFailureCode
    failed_month: Month | None = None
    detail: Annotated[str, Field(min_length=1, max_length=500)]


class StrategyJobActionV1(_LifecycleModel):
    job_id: Annotated[str, Field(min_length=1)]
    expected_version: Annotated[int, Field(gt=0)]
    legal_actions: tuple[str, ...]


class StrategyJobRestartV1(_LifecycleModel):
    source_job_id: Annotated[str, Field(min_length=1)]
    expected_version: Annotated[int, Field(gt=0)]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]


class StrategyJobDeletionV1(_LifecycleModel):
    job_id: Annotated[str, Field(min_length=1)]
    expected_version: Annotated[int, Field(gt=0)]


def requested_month_digest(
    profile_hash: str, months: tuple[str, ...], calendar_dataset_version: str
) -> str:
    return manifest_digest(
        {
            "schema_version": "initialization_requested_months.v1",
            "profile_hash": profile_hash,
            "months": months,
            "calendar_dataset_version": calendar_dataset_version,
        }
    )


__all__ = [
    "BacktestEnqueueResultV1",
    "BacktestRunV1",
    "BacktestSubmissionV1",
    "ClaimedStrategyJobV1",
    "InitializationEnqueueResultV1",
    "InitializationRunV1",
    "InitializationSubmissionV1",
    "JobFailureCode",
    "StrategyJobConflict",
    "StrategyJobCancellationV1",
    "StrategyJobError",
    "StrategyJobNotFound",
    "StrategyJobStatus",
    "StrategyJobFailureV1",
    "StrategyJobProgressV1",
    "StrategyJobActionV1",
    "StrategyJobRestartV1",
    "StrategyJobDeletionV1",
    "StrategyJobType",
    "StrategyJobV1",
    "requested_month_digest",
]
