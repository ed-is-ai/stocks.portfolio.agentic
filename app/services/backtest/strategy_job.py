"""Typed durable lifecycle contracts for Strategy Manager work."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_scan_record import FrozenDict
from app.services.backtest.trading_calendar import TradingCalendar


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class StrategyJobType(StrEnum):
    BOOTSTRAP = "bootstrap"
    INITIALIZATION = "initialization"
    PREPARATION = "preparation"
    BACKTEST = "backtest"


class BootstrapStage(StrEnum):
    """The closed, ordered stage sequence one Bootstrap activity walks."""

    QUALIFICATION = "qualification"
    ROSTER_CAPTURE = "roster_capture"
    PROFILE_ACTIVATION = "profile_activation"


class PreparationStage(StrEnum):
    """The closed, ordered stage sequence one Preparation activity walks."""

    EVIDENCE_SELECTION = "evidence_selection"
    FX_PINNING = "fx_pinning"
    MANIFEST_SEALING = "manifest_sealing"


#: The one progress mechanism the two stage-walking activity types use.
#: ``initialization``/``backtest`` keep reporting progress through
#: ``current_month`` instead -- there is never a second progress
#: mechanism for them.
STAGE_SEQUENCES: Mapping[StrategyJobType, tuple[str, ...]] = MappingProxyType(
    {
        StrategyJobType.BOOTSTRAP: tuple(stage.value for stage in BootstrapStage),
        StrategyJobType.PREPARATION: tuple(stage.value for stage in PreparationStage),
    }
)

#: Every legal ``current_stage`` value, in the closed-set order the
#: ``strategy_jobs.current_stage`` CHECK constraint mirrors.
STAGE_VALUES: tuple[str, ...] = tuple(
    stage for sequence in STAGE_SEQUENCES.values() for stage in sequence
)

#: The job types whose progress is a stage rather than a month.
STAGE_JOB_TYPES: frozenset[StrategyJobType] = frozenset(STAGE_SEQUENCES)


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


class PrerequisiteState(StrEnum):
    """Closed vocabulary for one readiness prerequisite's state."""

    MISSING = "missing"
    RUNNING = "running"
    READY = "ready"
    STALE_INCOMPATIBLE = "stale_incompatible"
    FAILED = "failed"
    INTEGRITY_ERROR = "integrity_error"


class WorkerState(StrEnum):
    """Closed vocabulary for the persisted worker lease state."""

    DISABLED = "disabled"
    UNAVAILABLE_INTERRUPTED = "unavailable_interrupted"
    BUSY = "busy"
    READY = "ready"


class RecoveryAction(StrEnum):
    """Closed vocabulary for the one recovery action per prerequisite."""

    SET_UP = "set_up"
    INITIALIZE = "initialize"
    CONFIGURE = "configure"
    RECONCILE_WORKER = "reconcile_worker"
    RETRY = "retry"
    NONE = "none"


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
    current_stage: str | None = None
    owner_instance_id: str | None = None
    lease_generation: Annotated[int, Field(gt=0)] | None = None
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
            self.claim_token is not None
            or self.current_month is not None
            or self.current_stage is not None
            or self.owner_instance_id is not None
        ):
            raise ValueError("queued jobs cannot carry worker ownership or progress")
        if self.status is StrategyJobStatus.RUNNING and self.claim_token is None:
            raise ValueError("running jobs require a claim token")
        if self.status.terminal and (
            self.current_month is not None or self.current_stage is not None
        ):
            raise ValueError("terminal jobs cannot carry current progress")
        if (self.owner_instance_id is None) != (self.lease_generation is None):
            raise ValueError("lease ownership requires both instance and generation")
        stage_typed = self.job_type in STAGE_JOB_TYPES
        if stage_typed and self.current_month is not None:
            raise ValueError(f"{self.job_type.value} jobs report stages, not months")
        if not stage_typed and self.current_stage is not None:
            raise ValueError(f"{self.job_type.value} jobs report months, not stages")
        if (
            self.current_stage is not None
            and self.current_stage not in STAGE_SEQUENCES[self.job_type]
        ):
            raise ValueError(
                f"{self.current_stage!r} is not a {self.job_type.value} stage"
            )
        failed = self.status is StrategyJobStatus.FAILED
        if failed != (
            self.failure_code is not None and self.failure_detail is not None
        ):
            raise ValueError("failure fields must match failed status")
        if not failed and self.failed_month is not None:
            raise ValueError("only failed jobs may carry a failed month")
        return self


class WorkerLeaseFenceV1(_LifecycleModel):
    """The ``(owner instance, generation)`` pair every job mutation is
    compare-and-swapped against.

    A write carrying a generation the persisted lease has already moved
    past belongs to an evicted owner, so its CAS predicate matches no row
    and the job is left exactly as it was.
    """

    instance_id: Annotated[str, Field(min_length=1)]
    generation: Annotated[int, Field(gt=0)]


class WorkerLeaseV1(_LifecycleModel):
    """The persisted singleton worker lease -- one process is "the worker".

    ``generation`` is monotonic across takeovers, never per job: job-level
    ``claim_token``/``status_version`` remain the per-row CAS fields and
    are now additionally fenced by this lease's identity.
    """

    instance_id: Annotated[str, Field(min_length=1)]
    generation: Annotated[int, Field(gt=0)]
    heartbeat_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _valid_window(self) -> "WorkerLeaseV1":
        for value in (self.heartbeat_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("lease timestamps must be timezone-aware")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("lease must expire after its heartbeat")
        return self

    @property
    def fence(self) -> WorkerLeaseFenceV1:
        """Return the ``(instance_id, generation)`` pair writes are fenced by."""
        return WorkerLeaseFenceV1(
            instance_id=self.instance_id, generation=self.generation
        )


class BootstrapRunV1(_LifecycleModel):
    """One ``bootstrap`` job's subtype identity row.

    Deliberately carries only the job identity: Bootstrap's real
    qualification / roster-identity-capture / profile-activation payload
    is Story 4.3's scope. What this story proves is that every
    ``strategy_jobs`` row has exactly one matching subtype row.
    """

    job_id: Annotated[str, Field(min_length=1)]


class PrerequisiteItemV1(_LifecycleModel):
    """One readiness prerequisite's typed state projection."""

    name: str
    state: PrerequisiteState
    reason: str
    last_verified_at: datetime | None
    recovery_action: RecoveryAction


class WorkerReadinessV1(_LifecycleModel):
    """The worker lease readiness projection."""

    state: WorkerState
    reason: str
    last_heartbeat_at: datetime | None
    recovery_action: RecoveryAction


class RecentJobFailureV1(_LifecycleModel):
    """One bounded recent job failure entry for diagnostics."""

    job_id: str
    job_type: StrategyJobType
    failure_code: JobFailureCode
    stage_or_month: str | None
    failed_at: datetime
    recovery_action: RecoveryAction


class StrategyReadinessV1(_LifecycleModel):
    """Six independent prerequisites + worker state + recent failures."""

    qualification: PrerequisiteItemV1
    roster: PrerequisiteItemV1
    active_profile: PrerequisiteItemV1
    coverage: PrerequisiteItemV1
    worker: WorkerReadinessV1
    discovery: PrerequisiteItemV1
    recent_failures: tuple[RecentJobFailureV1, ...]
    is_fixture: bool


class RunUniverseSelectionV1(_LifecycleModel):
    """One canonicalized universe selection for a Backtest Run."""

    profile_hash: Digest
    activation_seq: Annotated[int, Field(gt=0)]
    universe_schema: Literal["strategy_universe.v1"] = "strategy_universe.v1"
    universe_mode: Literal["selected-securities"] = "selected-securities"
    universe_parameter: Annotated[
        str, Field(min_length=1, pattern=r"^\S(?:.*\S)?$")
    ] = "security_ids"
    canonical_security_ids: tuple[str, ...]
    run_universe_digest: Digest

    @model_validator(mode="after")
    def _identity(self) -> "RunUniverseSelectionV1":
        from app.services.backtest.run_universe import (
            canonical_run_universe,
            run_universe_digest,
        )

        if (
            canonical_run_universe(self.canonical_security_ids)
            != self.canonical_security_ids
        ):
            raise ValueError("selected IDs are not canonical")
        if self.run_universe_digest != run_universe_digest(
            self.canonical_security_ids,
            universe_schema=self.universe_schema,
            mode=self.universe_mode,
            parameter=self.universe_parameter,
            profile_hash=self.profile_hash,
        ):
            raise ValueError("run universe digest is invalid")
        return self


class PreparationRunV1(_LifecycleModel):
    """One ``preparation`` job's subtype identity row.

    The evidence-selection / historical-FX-pinning / manifest-sealing
    payload is Story 4.6's scope; see :class:`BootstrapRunV1`.
    """

    job_id: Annotated[str, Field(min_length=1)]
    selection: RunUniverseSelectionV1 | None = None
    strategy_id: str | None = None
    strategy_api_version: int | None = None
    strategy_source_digest: Digest | None = None
    parameters: dict[str, object] = Field(default_factory=dict)
    start_month: Month | None = None
    end_month: Month | None = None
    base_currency: Literal["GBP", "USD"] | None = None
    starting_capital: Decimal | None = None


class PreparationSubmissionV1(_LifecycleModel):
    selection: RunUniverseSelectionV1
    strategy_id: Annotated[str, Field(min_length=1)]
    strategy_api_version: Annotated[int, Field(ge=1)]
    strategy_source_digest: Digest
    parameters: dict[str, object]
    start_month: Month
    end_month: Month
    base_currency: Literal["GBP", "USD"]
    starting_capital: Decimal = Field(gt=Decimal(0))
    parent_job_id: str | None = None
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _runtime_matches_selection(self) -> "PreparationSubmissionV1":
        TradingCalendar.months_inclusive(self.start_month, self.end_month)
        if self.parameters.get(self.selection.universe_parameter) != list(
            self.selection.canonical_security_ids
        ):
            raise ValueError("runtime universe does not match selected universe")
        return self

    def content_digest(self) -> str:
        value = self.model_dump(mode="python", exclude={"idempotency_key"})
        value["starting_capital"] = str(self.starting_capital)
        return manifest_digest(value)


class PreparationEnqueueResultV1(_LifecycleModel):
    job: StrategyJobV1
    preparation: PreparationRunV1


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
    mode: Literal["update", "rebuild"] = "rebuild"

    @model_validator(mode="after")
    def _valid_range(self) -> "InitializationRunV1":
        expected = TradingCalendar.months_inclusive(
            self.requested_start, self.requested_end
        )
        if self.requested_months != expected:
            raise ValueError("initialization requested months do not match its range")
        digest = requested_month_digest(
            self.profile_hash,
            expected,
            self.calendar_dataset_version,
            mode=self.mode,
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
    manifest_version: Literal["run_input_manifest.v1", "run_input_manifest.v2"] = (
        "run_input_manifest.v1"
    )
    universe_selection: RunUniverseSelectionV1 | None = None
    source_preparation_job_id: str | None = None

    def model_dump(self, **kwargs):  # preserve legacy V1 typed serialization
        if self.manifest_version == "run_input_manifest.v1":
            raw_exclude = kwargs.pop("exclude", None)
            exclude = set(raw_exclude or ()) | {
                "manifest_version",
                "universe_selection",
                "source_preparation_job_id",
            }
            return super().model_dump(exclude=exclude, **kwargs)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs) -> str:
        if self.manifest_version == "run_input_manifest.v1":
            exclude = set(kwargs.pop("exclude", None) or ()) | {
                "manifest_version",
                "universe_selection",
                "source_preparation_job_id",
            }
            return super().model_dump_json(exclude=exclude, **kwargs)
        return super().model_dump_json(**kwargs)

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
        is_v2 = self.manifest_version == "run_input_manifest.v2"
        if is_v2 != (self.universe_selection is not None):
            raise ValueError("run version provenance is invalid")
        selection = self.universe_selection
        if (
            is_v2
            and selection is not None
            and self.parameters.get(selection.universe_parameter)
            != list(selection.canonical_security_ids)
        ):
            raise ValueError("runtime universe does not match provenance")  # type: ignore[union-attr]
        return self


class ClaimedStrategyJobV1(_LifecycleModel):
    """One claimed job plus the exactly-one subtype row that matches it.

    ``lease_generation`` is the generation of the lease the claim was
    taken under, and is what a dispatched worker must present on every
    subsequent mutating write.
    """

    job: StrategyJobV1
    bootstrap: BootstrapRunV1 | None = None
    initialization: InitializationRunV1 | None = None
    preparation: PreparationRunV1 | None = None
    backtest: BacktestRunV1 | None = None
    claim_token: Annotated[str, Field(min_length=1)]
    lease_generation: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def _matches_job(self) -> "ClaimedStrategyJobV1":
        if (
            self.job.status is not StrategyJobStatus.RUNNING
            or self.job.claim_token != self.claim_token
        ):
            raise ValueError("claim does not own the running job")
        if self.lease_generation != self.job.lease_generation:
            raise ValueError("claim does not match the job's lease generation")
        present = {
            job_type: subtype
            for job_type, subtype in (
                (StrategyJobType.BOOTSTRAP, self.bootstrap),
                (StrategyJobType.INITIALIZATION, self.initialization),
                (StrategyJobType.PREPARATION, self.preparation),
                (StrategyJobType.BACKTEST, self.backtest),
            )
            if subtype is not None
        }
        matching = present.get(self.job.job_type)
        if matching is None or matching.job_id != self.job.id:
            raise ValueError(
                f"{self.job.job_type.value} claim requires its own subtype"
            )
        if len(present) != 1:
            raise ValueError(
                f"{self.job.job_type.value} claim must carry exactly one subtype"
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


class BootstrapEnqueueResultV1(_LifecycleModel):
    """One atomic Bootstrap submission outcome.

    A compatible active profile is a verified no-op. Every accepted create
    or exact replay otherwise carries the durable job and Bootstrap subtype.
    """

    no_op: bool
    job: StrategyJobV1 | None = None
    bootstrap: BootstrapRunV1 | None = None

    @model_validator(mode="after")
    def _consistent_result(self) -> "BootstrapEnqueueResultV1":
        if self.no_op:
            if self.job is not None or self.bootstrap is not None:
                raise ValueError("no-op bootstrap cannot contain a job")
        elif (
            self.job is None
            or self.bootstrap is None
            or self.bootstrap.job_id != self.job.id
        ):
            raise ValueError("accepted bootstrap requires matching job and subtype")
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
    manifest_version: Literal["run_input_manifest.v1", "run_input_manifest.v2"] = (
        "run_input_manifest.v1"
    )
    universe_selection: RunUniverseSelectionV1 | None = None
    source_preparation_job_id: str | None = None
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
        is_v2 = self.manifest_version == "run_input_manifest.v2"
        if is_v2 != (self.universe_selection is not None):
            raise ValueError("manifest version provenance is invalid")
        if is_v2 and (
            (self.source_preparation_job_id is None) == (self.parent_job_id is None)
        ):
            raise ValueError("V2 submission requires exactly one lineage")
        selection = self.universe_selection
        if (
            is_v2
            and selection is not None
            and self.parameters.get(selection.universe_parameter)
            != list(selection.canonical_security_ids)
        ):
            raise ValueError("runtime universe does not match provenance")  # type: ignore[union-attr]
        return self


class BootstrapSubmissionV1(_LifecycleModel):
    """One caller-validated Bootstrap setup submission."""

    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]
    parent_job_id: Annotated[str, Field(min_length=1)] | None = None

    @field_validator("idempotency_key")
    @classmethod
    def _non_blank_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("bootstrap idempotency_key must not be blank")
        return value

    def canonical_content_digest(self) -> str:
        """Return the versioned request identity, excluding the opaque key."""
        return manifest_digest(
            {
                "schema": "bootstrap-submission-v1",
                "parent_job_id": self.parent_job_id,
            }
        )


class InitializationSubmissionV1(_LifecycleModel):
    profile_hash: Digest
    requested_start: Month
    requested_end: Month
    calendar_dataset_version: Annotated[str, Field(min_length=1)]
    parent_job_id: str | None = None
    # gh-468: Update adopts unchanged members from the predecessor data
    # version; Rebuild resolves every member from scratch. The choice is part
    # of the enqueued job's identity via ``requested_month_digest``.
    mode: Literal["update", "rebuild"] = "rebuild"

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
    lease: WorkerLeaseFenceV1 | None = None
    month: Month


class StrategyJobFailureV1(_LifecycleModel):
    job_id: Annotated[str, Field(min_length=1)]
    claim_token: Annotated[str, Field(min_length=1)]
    expected_version: Annotated[int, Field(gt=0)]
    lease: WorkerLeaseFenceV1 | None = None
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
    profile_hash: str,
    months: tuple[str, ...],
    calendar_dataset_version: str,
    *,
    mode: str = "rebuild",
) -> str:
    """Return the initialization run's requested-month identity.

    gh-468: Update mode folds the path choice into the digest so restart and
    replay of an Update job cannot silently degrade into a Rebuild. Rebuild
    digests keep the pre-gh-468 payload exactly, so stored runs enqueued
    before the mode column existed still validate on load.
    """
    payload: dict[str, object] = {
        "schema_version": "initialization_requested_months.v1",
        "profile_hash": profile_hash,
        "months": months,
        "calendar_dataset_version": calendar_dataset_version,
    }
    if mode != "rebuild":
        payload["mode"] = mode
    return manifest_digest(payload)


__all__ = [
    "BacktestEnqueueResultV1",
    "BacktestRunV1",
    "BacktestSubmissionV1",
    "BootstrapEnqueueResultV1",
    "BootstrapRunV1",
    "BootstrapStage",
    "ClaimedStrategyJobV1",
    "InitializationEnqueueResultV1",
    "InitializationRunV1",
    "InitializationSubmissionV1",
    "JobFailureCode",
    "PreparationRunV1",
    "PreparationSubmissionV1",
    "PreparationEnqueueResultV1",
    "PreparationStage",
    "PrerequisiteItemV1",
    "PrerequisiteState",
    "RecentJobFailureV1",
    "RecoveryAction",
    "RunUniverseSelectionV1",
    "STAGE_JOB_TYPES",
    "STAGE_SEQUENCES",
    "STAGE_VALUES",
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
    "StrategyReadinessV1",
    "WorkerLeaseFenceV1",
    "WorkerLeaseV1",
    "WorkerReadinessV1",
    "WorkerState",
    "requested_month_digest",
]
