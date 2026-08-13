"""Deterministic month-boundary orchestration for historical initialization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Protocol

from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_data_qualification import (
    REQUEST_CONTRACT_VERSION,
    ProviderFailure,
)
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.historical_scan_reconstruction import (
    CALENDAR_DATASET_VERSION,
    HistoricalScanReconstructor,
    ReconstructionError,
    ReconstructionRequestV1,
    canonical_calendar_digest,
)
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
from app.services.backtest.reconstruction_roster import (
    CapturedRosterMemberV1,
    CapturedRosterV1,
)
from app.services.backtest.snapshot_profile import (
    FULL_HISTORY_START,
    IntervalReadinessV1,
    MonthlySnapshotCommitV1,
    SnapshotContractError,
    SnapshotMemberV1,
    SnapshotProfileV1,
    build_before_first_provider_observation,
)
from app.services.backtest.source_manifest import (
    DetectorInputIdentityV1,
    ReconstructionInputManifestV1,
    detector_source_manifests,
    yfinance_ingestion_source_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.trading_calendar import CalendarContractError

from app.services.backtest.strategy_job import (
    InitializationRunV1,
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
)


class InitializationMonthError(RuntimeError):
    """One safe, closed failure emitted by month preparation."""

    def __init__(self, code: JobFailureCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class ResolvedSnapshotMember:
    member: SnapshotMemberV1
    record: HistoricalScanRecordV1 | None


class CanonicalSnapshotMonthProcessor:
    """Compose existing evidence/reconstruction APIs into one Ready month."""

    _TIMEZONES = {
        "XNAS": "America/New_York",
        "XNYS": "America/New_York",
        "XLON": "Europe/London",
    }

    def __init__(
        self,
        *,
        job_id: str,
        claim_token: str,
        profile: SnapshotProfileV1,
        roster: CapturedRosterV1,
        backtest_repository: BacktestRepository,
        price_repository: HistoricalPriceRepository,
        evidence_adapter: YFinanceHistoricalEvidenceAdapter | None = None,
        reconstructor: HistoricalScanReconstructor | None = None,
        calendar: TradingCalendar | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        project_root: Path | None = None,
    ) -> None:
        if profile.roster_digest != roster.roster_digest:
            raise ValueError("snapshot profile and reconstruction roster differ")
        self._job_id = job_id
        self._claim_token = claim_token
        self._profile = profile
        self._roster = roster
        self._backtest_repository = backtest_repository
        self._price_repository = price_repository
        self._evidence_adapter = evidence_adapter or YFinanceHistoricalEvidenceAdapter()
        self._reconstructor = reconstructor or HistoricalScanReconstructor(
            backtest_repository
        )
        self._calendar = calendar or TradingCalendar()
        self._clock = clock
        self._project_root = project_root or Path(__file__).resolve().parents[3]
        try:
            roster_payload = json.loads(roster.canonical_manifest_json)
            self._alias_revision = str(roster_payload["alias_revision"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("reconstruction roster alias evidence is invalid") from exc

    def __call__(self, snapshot_month: str) -> None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise InitializationMonthError(
                JobFailureCode.INTEGRITY_ERROR, "Initialization clock is invalid"
            )
        try:
            sessions = self._calendar.month_sessions(
                tuple(member.mic for member in self._roster.members),
                snapshot_month,
                as_of=now.date(),
            )
            resolved = tuple(
                self._resolve_member(member, snapshot_month, sessions[member.mic], now)
                for member in sorted(
                    self._roster.members, key=lambda item: item.security_id
                )
            )
            members = tuple(item.member for item in resolved)
            records = tuple(item.record for item in resolved if item.record is not None)
            commit = MonthlySnapshotCommitV1.build(
                profile=self._profile,
                snapshot_month=snapshot_month,
                provenance_quality="best_effort_reconstructed",
                members=members,
                records=records,
                committed_at=now.astimezone(timezone.utc),
                as_of=now.date(),
            )
            for member in members:
                self._price_repository.pin(
                    "snapshot",
                    f"{self._profile.profile_hash}:{snapshot_month}:{member.security_id}",
                    member.provider_data_revision,
                )
            self._backtest_repository.commit_snapshot_month(
                commit,
                self._price_repository,
                job_claim=(self._job_id, self._claim_token),
            )
        except InitializationMonthError:
            raise
        except ProviderFailure as exc:
            raise InitializationMonthError(
                JobFailureCode(exc.code.value), str(exc)
            ) from exc
        except ReconstructionError as exc:
            raise InitializationMonthError(
                JobFailureCode(exc.code), exc.detail
            ) from exc
        except CalendarContractError as exc:
            raise InitializationMonthError(
                JobFailureCode.CALENDAR_ERROR,
                "Historical calendar could not resolve the requested month",
            ) from exc
        except (BacktestIntegrityError, SnapshotContractError) as exc:
            code = getattr(exc, "code", "integrity_error")
            try:
                failure_code = JobFailureCode(str(code))
            except ValueError:
                failure_code = JobFailureCode.INTEGRITY_ERROR
            raise InitializationMonthError(
                failure_code, "Historical month could not be committed"
            ) from exc
        except Exception as exc:
            raise InitializationMonthError(
                JobFailureCode.INTEGRITY_ERROR,
                "Historical month failed integrity validation",
            ) from exc

    def _resolve_member(
        self,
        member: CapturedRosterMemberV1,
        snapshot_month: str,
        target_session: date,
        now: datetime,
    ) -> ResolvedSnapshotMember:
        end_exclusive = date(now.year, now.month, 1)
        expected_sessions = self._calendar.sessions_in_range(
            member.mic, FULL_HISTORY_START, end_exclusive
        )
        request = HistoricalEvidenceRequest(
            security_id=member.security_id,
            alias_revision=self._alias_revision,
            symbol=member.provider_symbol,
            start=FULL_HISTORY_START,
            end=end_exclusive,
            expected_currency=member.currency,
            expected_quote_unit=member.quote_unit,
            expected_timezone=self._TIMEZONES[member.mic],
            expected_sessions=expected_sessions,
            allowed_observed_symbols=(member.provider_symbol,),
            allow_missing_prefix=True,
        )
        evidence = self._evidence_for(member, request)
        first_observed = date.fromisoformat(str(evidence.rows[0]["session"]))

        if target_session < first_observed:
            alias_from, alias_to = self._backtest_repository.effective_alias_bounds(
                alias_revision=self._alias_revision,
                security_id=member.security_id,
                mic=member.mic,
                observed_symbol=evidence.observed_symbol,
                session_date=target_session,
            )
            acquired = self._price_repository.acquisition_times(evidence.data_revision)
            if not acquired:
                raise InitializationMonthError(
                    JobFailureCode.INTEGRITY_ERROR,
                    "Historical evidence acquisition audit is missing",
                )
            proof = build_before_first_provider_observation(
                evidence=evidence,
                snapshot_month=snapshot_month,
                target_session=target_session,
                mic=member.mic,  # type: ignore[arg-type]
                alias_revision=self._alias_revision,
                alias_effective_from=alias_from,
                alias_effective_to=alias_to,
                calendar_dataset_version=self._profile.calendar_dataset_version,
                calendar_dataset_digest=self._profile.calendar_dataset_digest,
                acquired_at=datetime.fromisoformat(acquired[0]).astimezone(
                    timezone.utc
                ),
            )
            return ResolvedSnapshotMember(
                SnapshotMemberV1.legitimate_exclusion(proof), None
            )

        input_manifest = self._input_manifest(
            member, snapshot_month, target_session, evidence
        )
        result = self._reconstructor.reconstruct(
            ReconstructionRequestV1(
                security_id=member.security_id,
                observed_symbol=evidence.observed_symbol,
                mic=member.mic,
                snapshot_month=snapshot_month,
                as_of_session_date=target_session,
                identity_candidates=(member.security_id,),
                roster=self._roster,
                evidence=evidence,
                input_manifest=input_manifest,
            )
        )
        return ResolvedSnapshotMember(
            SnapshotMemberV1.valid_scan(result.record), result.record
        )

    def _evidence_for(self, member, request: HistoricalEvidenceRequest):
        evidence = self._price_repository.find_request(
            security_id=member.security_id,
            requested_symbol=member.provider_symbol,
            alias_revision=self._alias_revision,
            start=FULL_HISTORY_START.isoformat(),
            end=request.end.isoformat(),
            request_contract_version=REQUEST_CONTRACT_VERSION,
        )
        if evidence is None:
            payload = self._evidence_adapter.fetch(request)
            revision = self._price_repository.commit(payload)
            evidence = self._price_repository.verify(revision)
        self._validate_cached_evidence(evidence, request)
        return evidence

    @staticmethod
    def _validate_cached_evidence(evidence, request: HistoricalEvidenceRequest) -> None:
        if (
            evidence.security_id != request.security_id
            or evidence.alias_revision != request.alias_revision
            or evidence.requested_symbol != request.symbol
            or evidence.observed_symbol not in request.allowed_observed_symbols
            or evidence.currency != request.expected_currency
            or evidence.quote_unit != request.expected_quote_unit
            or evidence.exchange_timezone != request.expected_timezone
        ):
            raise InitializationMonthError(
                JobFailureCode.PROVIDER_CONTRACT_ERROR,
                "Cached historical evidence does not match the pinned security",
            )
        sessions = tuple(
            date.fromisoformat(str(row["session"])) for row in evidence.rows
        )
        expected = request.expected_sessions
        if not sessions or sessions != expected[-len(sessions) :]:
            raise InitializationMonthError(
                JobFailureCode.REQUIRED_DATA_MISSING,
                "Required historical data is unavailable",
            )

    def _input_manifest(
        self, member, snapshot_month: str, target_session: date, evidence
    ) -> ReconstructionInputManifestV1:
        manifests = detector_source_manifests(self._project_root)
        return ReconstructionInputManifestV1(
            schema_version="reconstruction_input_manifest.v1",
            security_id=member.security_id,
            snapshot_month=snapshot_month,
            as_of_session_date=target_session,
            provider_data_revision=evidence.data_revision,
            evidence_start=date.fromisoformat(evidence.start),
            evidence_end=date.fromisoformat(evidence.end),
            provider_request_contract_version=evidence.request_contract_version,
            provider_evidence_manifest_digest=evidence.data_revision,
            market_plane_policy_version=PRICE_VOLUME_PLANE_VERSION,
            alias_revision=self._alias_revision,
            roster_digest=self._roster.roster_digest,
            calendar_dataset_version=CALENDAR_DATASET_VERSION,
            calendar_dataset_digest=canonical_calendar_digest(),
            yfinance_ingestion_version=yfinance_ingestion_source_manifest(
                self._project_root
            ).digest,
            record_schema_version="historical_scan_record.v1",
            reconstructability_policy_version="reconstructability.v1",
            detectors=tuple(
                DetectorInputIdentityV1(
                    detector_id=detector.detector_id,
                    detector_api_version=detector.detector_api_version,
                    detector_version=manifests[detector.detector_id].digest,
                    configuration=dict(detector.configuration),
                )
                for detector in DETECTOR_REGISTRY
            ),
        )


class InitializationRepository(Protocol):
    def strategy_job(self, job_id: str) -> StrategyJobV1: ...

    def initialization_run(self, job_id: str) -> InitializationRunV1: ...

    def interval_readiness(
        self, profile_hash: str, start_month: str, end_month: str
    ) -> IntervalReadinessV1: ...

    def set_strategy_job_current_month(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        month: str,
    ) -> StrategyJobV1: ...

    def fail_claimed_strategy_job(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        failure_code: JobFailureCode,
        failed_month: str | None,
        detail: str,
    ) -> StrategyJobV1: ...

    def cancel_claimed_strategy_job(
        self, job_id: str, claim_token: str, *, expected_version: int
    ) -> StrategyJobV1: ...

    def complete_claimed_initialization_job(
        self, job_id: str, claim_token: str, *, expected_version: int
    ) -> StrategyJobV1: ...


class HistoricalInitializationEngine:
    """Run one claimed initialization in stable month order.

    The month processor owns evidence acquisition, reconstruction, and the
    Story 1.6 complete-month transaction. This engine owns only lifecycle
    boundaries and never checkpoints partial month work.
    """

    def __init__(
        self,
        repository: InitializationRepository,
        month_processor: Callable[[str], None],
        *,
        qualification_check: Callable[[], bool] = lambda: True,
        profile_check: Callable[[str], bool] = lambda _profile_hash: True,
    ) -> None:
        self._repository = repository
        self._month_processor = month_processor
        self._qualification_check = qualification_check
        self._profile_check = profile_check

    def run(self, job_id: str, claim_token: str) -> StrategyJobV1:
        job = self._repository.strategy_job(job_id)
        if not self._owns(job, claim_token):
            return job
        initialization = self._repository.initialization_run(job_id)
        if job.job_type not in {StrategyJobType.INITIALIZATION, "initialization"}:
            return self._fail_or_cancel(
                job,
                claim_token,
                JobFailureCode.INTEGRITY_ERROR,
                None,
                "Worker job type does not match initialization",
            )
        if not self._qualification_check():
            return self._fail_or_cancel(
                job,
                claim_token,
                JobFailureCode.PROVIDER_CONTRACT_ERROR,
                None,
                "Historical data contract is not qualified",
            )
        if not self._profile_check(initialization.profile_hash):
            return self._fail_or_cancel(
                job,
                claim_token,
                JobFailureCode.INTEGRITY_ERROR,
                None,
                "Pinned snapshot profile is unavailable",
            )

        for month in initialization.requested_months:
            job = self._repository.strategy_job(job_id)
            if not self._owns(job, claim_token):
                return job
            if job.cancel_requested_at is not None:
                return self._repository.cancel_claimed_strategy_job(
                    job_id, claim_token, expected_version=job.status_version
                )
            try:
                job = self._repository.set_strategy_job_current_month(
                    job_id,
                    claim_token,
                    expected_version=job.status_version,
                    month=month,
                )
            except StrategyJobConflict:
                current = self._repository.strategy_job(job_id)
                if self._owns(current, claim_token) and (
                    current.cancel_requested_at is not None
                ):
                    return self._repository.cancel_claimed_strategy_job(
                        job_id,
                        claim_token,
                        expected_version=current.status_version,
                    )
                return current
            try:
                readiness = self._repository.interval_readiness(
                    initialization.profile_hash, month, month
                )
                if not readiness.ready:
                    self._month_processor(month)
            except InitializationMonthError as exc:
                current = self._repository.strategy_job(job_id)
                if not self._owns(current, claim_token):
                    return current
                return self._fail_or_cancel(
                    current, claim_token, exc.code, month, exc.detail
                )
            except Exception:
                current = self._repository.strategy_job(job_id)
                if not self._owns(current, claim_token):
                    return current
                return self._fail_or_cancel(
                    current,
                    claim_token,
                    JobFailureCode.INTEGRITY_ERROR,
                    month,
                    "Historical initialization failed integrity validation",
                )

            job = self._repository.strategy_job(job_id)
            if not self._owns(job, claim_token):
                return job
            if job.cancel_requested_at is not None:
                return self._repository.cancel_claimed_strategy_job(
                    job_id, claim_token, expected_version=job.status_version
                )

        job = self._repository.strategy_job(job_id)
        if not self._owns(job, claim_token):
            return job
        if job.cancel_requested_at is not None:
            return self._repository.cancel_claimed_strategy_job(
                job_id, claim_token, expected_version=job.status_version
            )
        try:
            return self._repository.complete_claimed_initialization_job(
                job_id, claim_token, expected_version=job.status_version
            )
        except StrategyJobConflict:
            return self._repository.strategy_job(job_id)

    def _fail(
        self,
        job,
        claim_token: str,
        code: JobFailureCode,
        failed_month: str | None,
        detail: str,
    ):
        return self._repository.fail_claimed_strategy_job(
            job.id,
            claim_token,
            expected_version=job.status_version,
            failure_code=code,
            failed_month=failed_month,
            detail=detail,
        )

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
            return self._fail(job, claim_token, code, failed_month, detail)
        except StrategyJobConflict:
            current = self._repository.strategy_job(job.id)
            if (
                self._owns(current, claim_token)
                and current.cancel_requested_at is not None
            ):
                return self._repository.cancel_claimed_strategy_job(
                    current.id,
                    claim_token,
                    expected_version=current.status_version,
                )
            return current

    @staticmethod
    def _owns(job, claim_token: str) -> bool:
        return (
            job.status in {StrategyJobStatus.RUNNING, "running"}
            and job.claim_token == claim_token
        )


__all__ = ["HistoricalInitializationEngine", "InitializationMonthError"]
