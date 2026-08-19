"""Claimed Strategy Manager worker module entry point."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Protocol

from app.core import config
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalPriceRepository,
    StoredHistoricalEvidence,
)
from app.services.backtest.backtest_engine import (
    EquityCurvePointV1,
    EntryFillEventV1,
    ExitFillEventV1,
    SecurityMarketDataV1,
    SimulationError,
    SimulationErrorCode,
    TradeLogEvent,
    run_simulation,
)
from app.services.backtest.historical_initialization_engine import (
    CanonicalSnapshotMonthProcessor,
    HistoricalInitializationEngine,
)
from app.services.backtest.market_view import MarketView
from app.services.backtest.reconstruction_roster import CapturedRosterV1
from app.services.backtest.run_input_manifest import (
    PinnedSecurityEvidenceV1,
    RunInputManifestV1,
)
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    discover_strategies,
)
from app.services.backtest.strategy_job import (
    BacktestRunV1,
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
)
from app.services.backtest.strategy_protocol import StrategyProtocolV1


class WorkerResult(Protocol):
    @property
    def status(self) -> StrategyJobStatus: ...


class WorkerEngine(Protocol):
    def run(self, job_id: str, claim_token: str) -> WorkerResult: ...


def build_worker_repository() -> BacktestRepository:
    repository = BacktestRepository(db.make_connect(lambda: str(config.BACKTEST_DB)))
    repository.ensure_schema()
    return repository


def build_initialization_engine(
    job_id: str, claim_token: str, backtest: BacktestRepository
) -> HistoricalInitializationEngine:
    """Build independent schema-ready repositories for one claimed child."""
    prices = HistoricalPriceRepository(
        db.make_connect(lambda: str(config.HISTORICAL_PRICE_CACHE))
    )
    prices.ensure_schema()
    initialization = backtest.initialization_run(job_id)
    profile = backtest.snapshot_profile(initialization.profile_hash)
    if profile is None:
        raise RuntimeError("Pinned snapshot profile is unavailable")
    roster_json = backtest.roster_manifest_json(profile.roster_digest)
    if roster_json is None:
        raise RuntimeError("Pinned reconstruction roster is unavailable")
    roster = CapturedRosterV1.from_json(profile.roster_digest, roster_json)
    processor = CanonicalSnapshotMonthProcessor(
        job_id=job_id,
        claim_token=claim_token,
        profile=profile,
        roster=roster,
        backtest_repository=backtest,
        price_repository=prices,
    )

    def qualified() -> bool:
        return (
            backtest.current_qualification_contract_digest()
            == initialization.qualification_contract_digest
        )

    def profile_is_current(profile_hash: str) -> bool:
        try:
            current = backtest.snapshot_profile(profile_hash)
            return (
                current is not None
                and current.calendar_dataset_version
                == initialization.calendar_dataset_version
            )
        except Exception:
            return False

    return HistoricalInitializationEngine(
        backtest,
        processor,
        qualification_check=qualified,
        profile_check=profile_is_current,
    )


# ---------------------------------------------------------------------------
# Story 2.6: Backtest engine factory, staging-sink adapter, and progress/
# cancellation orchestration.
# ---------------------------------------------------------------------------

#: The presentational (non-canonical, not digest-covered) portfolio-state
#: schema version staged alongside each session's events/curve -- purely
#: for a future in-progress view (Story 2.8); ``complete_claimed_backtest_
#: job`` never reads it back.
_PORTFOLIO_STATE_SCHEMA_VERSION = "backtest_portfolio_state.v1"

#: Maps ``SimulationErrorCode``/reused policy-module codes to the closed
#: ``JobFailureCode`` taxonomy (Story 2.4/2.5 triage precedent, see the
#: spec's Design Notes): structurally "tampered or missing pinned
#: evidence" or "arithmetic/invariant failure" -> ``integrity_error``; a
#: genuinely missing required data point -> ``required_data_missing``; an
#: evidence/runtime shape the engine does not support -> ``provider_
#: contract_error``. Never invents an eighth code -- anything unmapped
#: falls back to ``integrity_error``, with the original code/message
#: always preserved verbatim in ``failure_detail``.
_SIMULATION_FAILURE_CODES: dict[str, JobFailureCode] = {
    SimulationErrorCode.MISSING_REQUIRED_OPEN.value: JobFailureCode.REQUIRED_DATA_MISSING,
    SimulationErrorCode.MISSING_REQUIRED_CLOSE.value: JobFailureCode.REQUIRED_DATA_MISSING,
    "fx_missing": JobFailureCode.REQUIRED_DATA_MISSING,
    SimulationErrorCode.UNSUPPORTED_EXCHANGE_TIMEZONE.value: (
        JobFailureCode.PROVIDER_CONTRACT_ERROR
    ),
    "unsupported_corporate_action": JobFailureCode.PROVIDER_CONTRACT_ERROR,
    "unsupported_quote_unit": JobFailureCode.PROVIDER_CONTRACT_ERROR,
    "unsupported_currency": JobFailureCode.PROVIDER_CONTRACT_ERROR,
}


def _map_simulation_failure_code(code: str) -> JobFailureCode:
    return _SIMULATION_FAILURE_CODES.get(code, JobFailureCode.INTEGRITY_ERROR)


def _safe_detail(code: str, message: str) -> str:
    """Losslessly preserve the original engine error's own code/message
    (Design Notes) inside the 1-500 character ``failure_detail`` bound."""
    if not code and not message:
        return "backtest simulation failed"
    return f"{code}: {message}".strip()[:500]


class BacktestResolutionError(Exception):
    """One safe, closed, pre-staging resolution failure (AC 2, 3)."""

    def __init__(self, code: JobFailureCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class _BacktestCancelled(Exception):
    """Raised by the progress observer to unwind ``run_simulation`` the
    instant a running cancellation is detected at a month boundary --
    never caught inside the engine itself, only by the orchestrator
    below, which performs the actual atomic cancel transition."""


class _BacktestOwnershipLost(Exception):
    """Raised by the progress observer/sink when this attempt no longer
    owns its claim (stale token/version) -- the orchestrator re-reads and
    returns authoritative state rather than attempting any further write."""


class _BacktestEngineDefect(Exception):
    """Raised by the progress observer when the engine reports a month
    outside the Strategy Run's own pinned ``start_month``/``end_month``
    range -- the manifest's normalized range is pinned to match that
    range exactly, so this is only reachable via a genuine engine defect,
    never a legitimate CAS race. Caught by the orchestrator as a real,
    diagnosable failure rather than silently stranding the job via
    ``_BacktestOwnershipLost``."""


@dataclass
class _ClaimState:
    """Mutable claim-token/status-version cell the sink and progress
    observer share -- ``set_strategy_job_current_month``'s own CAS write
    bumps ``status_version`` at each month boundary, and every staging
    write after that must use the bumped value (Design Notes)."""

    job_id: str
    claim_token: str
    status_version: int


@dataclass
class _StagingSink:
    """Adapter satisfying ``backtest_engine.SessionBatchSink`` by calling
    ``write_backtest_staging`` (Story 2.5) once per session.

    ``write_backtest_staging`` is a full-replace compare-and-swap write,
    not an append (Design Notes) -- this adapter accumulates the complete
    session/event/curve history itself and republishes the whole thing on
    every call.
    """

    repository: BacktestRepository
    state: _ClaimState
    events: list[TradeLogEvent] = field(default_factory=list)
    equity_curve: list[EquityCurvePointV1] = field(default_factory=list)
    open_positions: dict[str, Decimal] = field(default_factory=dict)

    def publish_session(
        self,
        *,
        session: date,
        events: tuple[TradeLogEvent, ...],
        equity_point: EquityCurvePointV1,
    ) -> None:
        del session  # already carried by equity_point.session
        self.events.extend(events)
        self.equity_curve.append(equity_point)
        for event in events:
            if isinstance(event, EntryFillEventV1):
                self.open_positions[event.security_id] = Decimal(event.shares)
            elif isinstance(event, ExitFillEventV1):
                self.open_positions.pop(event.security_id, None)
        portfolio_state = {
            "cash": str(equity_point.cash_base),
            "positions": [
                {"security_id": security_id, "shares": str(shares)}
                for security_id, shares in sorted(self.open_positions.items())
            ],
        }
        try:
            self.repository.write_backtest_staging(
                self.state.job_id,
                claim_token=self.state.claim_token,
                expected_version=self.state.status_version,
                state_schema_version=_PORTFOLIO_STATE_SCHEMA_VERSION,
                portfolio_state=portfolio_state,
                events=tuple(self.events),
                equity_curve=tuple(self.equity_curve),
                final_cash_base=equity_point.cash_base,
            )
        except StrategyJobConflict as exc:
            # ``write_backtest_staging``'s own CAS predicate rejects any
            # write once ``cancel_requested_at`` is set -- even mid-month,
            # before the next month-boundary check would otherwise notice
            # it. Distinguish that from a genuine ownership loss (a stale
            # token/version because another worker/attempt now owns this
            # job) so a mid-month cancellation still reaches the atomic
            # cancel transition rather than silently stranding the job.
            current = self.repository.strategy_job(self.state.job_id)
            if (
                current.status is StrategyJobStatus.RUNNING
                and current.claim_token == self.state.claim_token
                and current.cancel_requested_at is not None
            ):
                raise _BacktestCancelled(str(exc)) from exc
            raise _BacktestOwnershipLost(str(exc)) from exc


@dataclass
class _ProgressObserver:
    """Satisfies ``backtest_engine.MonthBoundaryObserver`` -- before the
    engine simulates any session in a new month, re-reads authoritative
    job state, unwinds via a running cancellation if requested (never
    publishing that later month), and otherwise CAS-records the new
    current month (AC 4, 5)."""

    repository: BacktestRepository
    state: _ClaimState
    backtest: BacktestRunV1

    def on_month_boundary(self, *, month: str) -> None:
        current = self.repository.strategy_job(self.state.job_id)
        if (
            current.status is not StrategyJobStatus.RUNNING
            or current.claim_token != self.state.claim_token
        ):
            raise _BacktestOwnershipLost("worker no longer owns this claim")
        if current.cancel_requested_at is not None:
            raise _BacktestCancelled("cancellation requested at a month boundary")
        if not (self.backtest.start_month <= month <= self.backtest.end_month):
            raise _BacktestEngineDefect(
                f"engine reported month {month!r} outside pinned range "
                f"{self.backtest.start_month}..{self.backtest.end_month}"
            )
        try:
            updated = self.repository.set_strategy_job_current_month(
                self.state.job_id,
                self.state.claim_token,
                expected_version=current.status_version,
                month=month,
            )
        except StrategyJobConflict as exc:
            raise _BacktestOwnershipLost(str(exc)) from exc
        self.state.status_version = updated.status_version


def _resolve_strategy_descriptor(strategy_id: str) -> StrategyDescriptorV1:
    result = discover_strategies(config.SKILLS_DIR)
    for descriptor in result.strategies:
        if descriptor.strategy_id == strategy_id:
            return descriptor
    raise BacktestResolutionError(
        JobFailureCode.INTEGRITY_ERROR,
        f"Strategy is no longer discoverable: {strategy_id}",
    )


def _load_strategy_instance(runtime_path: Path) -> StrategyProtocolV1:
    """Dynamically import and instantiate one Strategy's runtime module.

    Story 2.2's ``skill_discovery.py`` never imports a Strategy's
    ``scripts/strategy.py`` (discovery is metadata-only); this is the
    worker's own, deliberately minimal loading convention -- mirroring
    ``detectors.py``'s existing ``spec_from_file_location`` pattern -- for
    the one genuinely new integration surface this story owns: turning a
    resolved, pinned Strategy identity into a live ``StrategyProtocolV1``
    instance. Requires the module to define exactly one top-level class
    implementing the three-method protocol; no naming convention is
    otherwise assumed.
    """
    spec = spec_from_file_location(
        f"_strategy_runtime_{abs(hash(str(runtime_path)))}", runtime_path
    )
    if spec is None or spec.loader is None:
        raise BacktestResolutionError(
            JobFailureCode.INTEGRITY_ERROR,
            f"Strategy runtime entrypoint is unavailable: {runtime_path}",
        )
    module = module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise BacktestResolutionError(
            JobFailureCode.INTEGRITY_ERROR,
            f"Strategy runtime entrypoint failed to load: {exc}",
        ) from exc
    candidates = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and obj.__module__ == module.__name__
        and hasattr(obj, "entry_signals")
        and hasattr(obj, "exit_signals")
        and hasattr(obj, "position_size")
    ]
    if len(candidates) != 1:
        raise BacktestResolutionError(
            JobFailureCode.INTEGRITY_ERROR,
            "Strategy runtime must define exactly one StrategyProtocolV1 "
            f"implementation, found {len(candidates)}",
        )
    try:
        instance = candidates[0]()
    except Exception as exc:
        raise BacktestResolutionError(
            JobFailureCode.INTEGRITY_ERROR,
            f"Strategy runtime could not be instantiated: {exc}",
        ) from exc
    if not isinstance(instance, StrategyProtocolV1):
        raise BacktestResolutionError(
            JobFailureCode.INTEGRITY_ERROR,
            "Strategy runtime does not satisfy StrategyProtocolV1",
        )
    return instance


class BacktestExecutionEngine:
    """Run one claimed Backtest end to end (AC 2-6).

    Resolution (pinned manifest/evidence/Strategy identity) happens
    inside :meth:`run`, not the ``build_backtest_engine`` factory --
    mirroring ``HistoricalInitializationEngine``'s own
    qualification/profile checks -- so every "expected" launch-time
    failure (missing/tampered evidence, a Strategy no longer
    discoverable) maps to a specific, safe ``JobFailureCode`` rather than
    the generic construction-failure fallback ``worker.main()`` uses for
    genuinely unexpected configuration bugs.
    """

    def __init__(
        self,
        repository: BacktestRepository,
        backtest: BacktestRunV1,
        prices: HistoricalPriceRepository,
        project_root: Path,
    ) -> None:
        self._repository = repository
        self._backtest = backtest
        self._prices = prices
        self._project_root = project_root

    def run(self, job_id: str, claim_token: str) -> StrategyJobV1:
        job = self._repository.strategy_job(job_id)
        if not self._owns(job, claim_token):
            return job
        if job.job_type is not StrategyJobType.BACKTEST:
            return self._fail_or_cancel(
                job,
                claim_token,
                JobFailureCode.INTEGRITY_ERROR,
                None,
                "Worker job type does not match backtest",
            )

        try:
            manifest, strategy, security_market_data, fx_evidence = self._resolve()
        except BacktestResolutionError as exc:
            current = self._repository.strategy_job(job_id)
            if not self._owns(current, claim_token):
                return current
            return self._fail_or_cancel(
                current, claim_token, exc.code, None, exc.detail
            )
        except EvidenceMissingError as exc:
            current = self._repository.strategy_job(job_id)
            if not self._owns(current, claim_token):
                return current
            return self._fail_or_cancel(
                current,
                claim_token,
                JobFailureCode.REQUIRED_DATA_MISSING,
                None,
                _safe_detail("evidence_missing", str(exc)),
            )
        except Exception:
            current = self._repository.strategy_job(job_id)
            if not self._owns(current, claim_token):
                return current
            return self._fail_or_cancel(
                current,
                claim_token,
                JobFailureCode.INTEGRITY_ERROR,
                None,
                "Backtest worker configuration is invalid",
            )

        job = self._repository.strategy_job(job_id)
        if not self._owns(job, claim_token):
            return job
        if job.cancel_requested_at is not None:
            return self._repository.cancel_claimed_strategy_job(
                job_id, claim_token, expected_version=job.status_version
            )

        state = _ClaimState(
            job_id=job_id, claim_token=claim_token, status_version=job.status_version
        )
        sink = _StagingSink(repository=self._repository, state=state)
        observer = _ProgressObserver(
            repository=self._repository, state=state, backtest=self._backtest
        )

        def market_view_factory(session: date) -> MarketView:
            return MarketView(
                as_of_session=session,
                profile_hash=manifest.profile_hash,
                security_price_revisions={
                    item.security_id: item.price_revision
                    for item in manifest.securities
                },
                backtest_repo=self._repository,
                historical_price_repo=self._prices,
            )

        try:
            run_simulation(
                manifest=manifest,
                strategy=strategy,
                market_view_factory=market_view_factory,
                security_market_data=security_market_data,
                fx_evidence=fx_evidence,
                sink=sink,
                month_boundary_observer=observer,
            )
        except _BacktestCancelled:
            return self._cancel(job_id, claim_token)
        except _BacktestEngineDefect as exc:
            current = self._repository.strategy_job(job_id)
            if not self._owns(current, claim_token):
                return current
            return self._fail_or_cancel(
                current,
                claim_token,
                JobFailureCode.INTEGRITY_ERROR,
                None,
                _safe_detail(
                    "engine_defect",
                    f"engine reported a month outside the pinned range: {exc}",
                ),
            )
        except _BacktestOwnershipLost:
            return self._repository.strategy_job(job_id)
        except SimulationError as exc:
            current = self._repository.strategy_job(job_id)
            if not self._owns(current, claim_token):
                return current
            return self._fail_or_cancel(
                current,
                claim_token,
                _map_simulation_failure_code(exc.code),
                exc.month,
                _safe_detail(exc.code, str(exc)),
            )
        except Exception:
            current = self._repository.strategy_job(job_id)
            if not self._owns(current, claim_token):
                return current
            return self._fail_or_cancel(
                current,
                claim_token,
                JobFailureCode.INTEGRITY_ERROR,
                None,
                "Backtest simulation failed integrity validation",
            )

        job = self._repository.strategy_job(job_id)
        if not self._owns(job, claim_token):
            return job
        if job.cancel_requested_at is not None:
            return self._cancel(job_id, claim_token)
        try:
            return self._repository.complete_claimed_backtest_job(
                job_id, claim_token, expected_version=job.status_version
            )
        except StrategyJobConflict:
            # Ambiguous completion: re-read authoritative state rather
            # than assume the write failed -- never mark an already-
            # complete job failed, and never convert a pending
            # cancellation into an ordinary failure.
            current = self._repository.strategy_job(job_id)
            if current.status is StrategyJobStatus.COMPLETE:
                return current
            if self._owns(current, claim_token) and current.cancel_requested_at:
                return self._cancel(job_id, claim_token)
            return current

    def _resolve(
        self,
    ) -> tuple[
        RunInputManifestV1,
        StrategyProtocolV1,
        tuple[SecurityMarketDataV1, ...],
        StoredHistoricalEvidence | None,
    ]:
        manifest_json = self._repository.run_input_manifest_json(
            self._backtest.run_input_manifest_digest
        )
        if manifest_json is None:
            raise BacktestResolutionError(
                JobFailureCode.INTEGRITY_ERROR,
                "Pinned run input manifest is unavailable",
            )
        try:
            manifest = RunInputManifestV1.model_validate_json(manifest_json)
        except Exception as exc:
            raise BacktestResolutionError(
                JobFailureCode.INTEGRITY_ERROR,
                f"Pinned run input manifest is invalid: {exc}",
            ) from exc
        if manifest.digest() != self._backtest.run_input_manifest_digest:
            raise BacktestResolutionError(
                JobFailureCode.INTEGRITY_ERROR,
                "Pinned run input manifest does not match its own digest",
            )
        if self._repository.snapshot_profile(self._backtest.profile_hash) is None:
            raise BacktestResolutionError(
                JobFailureCode.INTEGRITY_ERROR,
                "Pinned snapshot profile is unavailable",
            )

        descriptor = _resolve_strategy_descriptor(self._backtest.strategy_id)
        if (
            descriptor.api_version != self._backtest.strategy_api_version
            or descriptor.source_digest != self._backtest.strategy_source_digest
        ):
            raise BacktestResolutionError(
                JobFailureCode.INTEGRITY_ERROR,
                "Pinned Strategy identity no longer matches the discovered Skill",
            )
        runtime_path = config.SKILLS_DIR / descriptor.runtime_path
        strategy = _load_strategy_instance(runtime_path)

        security_market_data = tuple(
            self._resolve_security(item) for item in manifest.securities
        )
        fx_evidence = self._resolve_fx_evidence(manifest)
        return manifest, strategy, security_market_data, fx_evidence

    def _resolve_security(self, item: PinnedSecurityEvidenceV1) -> SecurityMarketDataV1:
        evidence = self._prices.get(item.price_revision)
        if evidence.security_id != item.security_id:
            raise BacktestResolutionError(
                JobFailureCode.INTEGRITY_ERROR,
                f"Pinned price evidence does not match {item.security_id!r}",
            )
        return SecurityMarketDataV1(
            security_id=item.security_id, price_evidence=evidence
        )

    def _resolve_fx_evidence(
        self, manifest: RunInputManifestV1
    ) -> StoredHistoricalEvidence | None:
        revisions = {
            item.fx_revision for item in manifest.securities if item.fx_revision
        }
        if not revisions:
            return None
        if len(revisions) > 1:
            raise BacktestResolutionError(
                JobFailureCode.INTEGRITY_ERROR,
                "Backtest pins more than one distinct FX revision",
            )
        (revision,) = revisions
        return self._prices.get(revision)

    def _cancel(self, job_id: str, claim_token: str) -> StrategyJobV1:
        current = self._repository.strategy_job(job_id)
        if not self._owns(current, claim_token):
            return current
        try:
            return self._repository.cancel_claimed_strategy_job(
                job_id, claim_token, expected_version=current.status_version
            )
        except StrategyJobConflict:
            return self._repository.strategy_job(job_id)

    def _fail_or_cancel(
        self,
        job: StrategyJobV1,
        claim_token: str,
        code: JobFailureCode,
        failed_month: str | None,
        detail: str,
    ) -> StrategyJobV1:
        try:
            if job.cancel_requested_at is not None:
                return self._repository.cancel_claimed_strategy_job(
                    job.id, claim_token, expected_version=job.status_version
                )
            return self._repository.fail_claimed_strategy_job(
                job.id,
                claim_token,
                expected_version=job.status_version,
                failure_code=code,
                failed_month=failed_month,
                detail=detail,
            )
        except StrategyJobConflict:
            current = self._repository.strategy_job(job.id)
            if (
                self._owns(current, claim_token)
                and current.cancel_requested_at is not None
            ):
                return self._cancel(job.id, claim_token)
            return current

    @staticmethod
    def _owns(job: StrategyJobV1, claim_token: str) -> bool:
        return (
            job.status is StrategyJobStatus.RUNNING and job.claim_token == claim_token
        )


def build_backtest_engine(
    job_id: str, claim_token: str, backtest: BacktestRepository
) -> BacktestExecutionEngine:
    """Build one claimed Backtest's execution engine (AC 2, 3).

    Deliberately minimal: only wires independent schema-ready
    repositories and loads the pinned ``strategy_runs`` identity --
    every "expected" launch-time failure (missing/tampered evidence, a
    Strategy no longer discoverable, an incompatible pinned version) is
    resolved and safely mapped inside ``BacktestExecutionEngine.run``
    itself, not here, mirroring ``build_initialization_engine``'s own
    minimal-construction convention.
    """
    prices = HistoricalPriceRepository(
        db.make_connect(lambda: str(config.HISTORICAL_PRICE_CACHE))
    )
    prices.ensure_schema()
    strategy_run = backtest.strategy_run(job_id)
    return BacktestExecutionEngine(
        repository=backtest,
        backtest=strategy_run,
        prices=prices,
        project_root=config.ROOT_DIR,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    engine_factory: Callable[[str], WorkerEngine] | None = None,
    repository_factory: Callable[[], BacktestRepository] = build_worker_repository,
) -> int:
    parser = argparse.ArgumentParser(description="Run one claimed Strategy job")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--claim-token", required=True)
    args = parser.parse_args(argv)
    if engine_factory is not None:
        engine = engine_factory(args.job_id)
        result = engine.run(args.job_id, args.claim_token)
        return (
            0
            if result.status
            in {StrategyJobStatus.COMPLETE, StrategyJobStatus.CANCELLED}
            else 1
        )

    repository = repository_factory()
    job = repository.strategy_job(args.job_id)
    if (
        job.status is not StrategyJobStatus.RUNNING
        or job.claim_token != args.claim_token
    ):
        return 1
    try:
        if job.job_type is StrategyJobType.INITIALIZATION:
            engine: WorkerEngine = build_initialization_engine(
                args.job_id, args.claim_token, repository
            )
        elif job.job_type is StrategyJobType.BACKTEST:
            engine = build_backtest_engine(args.job_id, args.claim_token, repository)
        else:
            raise RuntimeError("Unsupported Strategy job type")
    except Exception:
        current = repository.strategy_job(args.job_id)
        if (
            current.status is StrategyJobStatus.RUNNING
            and current.claim_token == args.claim_token
        ):
            if current.cancel_requested_at is not None:
                repository.cancel_claimed_strategy_job(
                    current.id,
                    args.claim_token,
                    expected_version=current.status_version,
                )
            else:
                repository.fail_claimed_strategy_job(
                    current.id,
                    args.claim_token,
                    expected_version=current.status_version,
                    failure_code=JobFailureCode.INTEGRITY_ERROR,
                    failed_month=None,
                    detail="Strategy worker configuration is invalid",
                )
        return 1
    result = engine.run(args.job_id, args.claim_token)
    return (
        0
        if result.status
        in {
            StrategyJobStatus.COMPLETE,
            StrategyJobStatus.CANCELLED,
        }
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
