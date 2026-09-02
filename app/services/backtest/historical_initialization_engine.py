"""Deterministic month-boundary orchestration for historical initialization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Protocol, cast

from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.historical_price_repo import (
    HistoricalPriceRepository,
    StoredHistoricalEvidence,
)
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_data_qualification import (
    REQUEST_CONTRACT_VERSION,
    ProviderFailure,
)
from app.services.backtest.historical_price_evidence import (
    CANONICAL_EXCHANGE_SESSIONS_POLICY,
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
    rebind_historical_evidence_alias,
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
    adoption_gate_failures,
    build_before_first_provider_observation,
    build_incomplete_detector_history,
    build_insufficient_detector_history,
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
    WorkerLeaseFenceV1,
)

logger = logging.getLogger(__name__)


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
        "BATS": "America/New_York",
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
        lease: WorkerLeaseFenceV1 | None = None,
        mode: str = "rebuild",
    ) -> None:
        if profile.roster_digest != roster.roster_digest:
            raise ValueError("snapshot profile and reconstruction roster differ")
        if mode not in {"update", "rebuild"}:
            raise ValueError("initialization mode is invalid")
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
        self._lease = lease
        # gh-468: Update mode adopts unchanged members from the predecessor
        # data version's committed months; Rebuild resolves everything fresh.
        self._mode = mode
        self._predecessor_resolved = False
        self._predecessor: SnapshotProfileV1 | None = None
        # Every month in one initialization run asks for the same immutable
        # full-history evidence window. Re-validating and deserializing that
        # payload for each security/month pair dominates long reruns, while
        # keeping it in this process-local cache preserves the repository's
        # verification on the first read and does not alter persisted output.
        self._evidence_cache: dict[tuple[str, str, str, str, str], object] = {}
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
            adopted_from: str | None = None
            resolved: tuple[ResolvedSnapshotMember, ...] | None = None
            if self._mode == "update":
                try:
                    adopted_from, resolved = self._adopt_month(
                        snapshot_month, sessions, now
                    )
                except InitializationMonthError:
                    raise
                except Exception:
                    # Adoption output is byte-identical to a from-scratch
                    # month by construction, so an unexpected adoption error
                    # can always degrade to the full path without changing
                    # what is stored -- but it must be loud (gh-468).
                    logger.exception(
                        "Update-mode adoption of %s failed; resolving every "
                        "member from scratch",
                        snapshot_month,
                    )
                    adopted_from, resolved = None, None
            if resolved is None:
                resolved = tuple(
                    self._resolve_member(
                        member, snapshot_month, sessions[member.mic], now
                    )
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
                lease=self._lease,
                adopted_from_profile_hash=adopted_from,
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
            logger.exception(
                "Historical initialization month %s failed at commit: %s",
                snapshot_month,
                exc,
            )
            code = getattr(exc, "code", "integrity_error")
            try:
                failure_code = JobFailureCode(str(code))
            except ValueError:
                failure_code = JobFailureCode.INTEGRITY_ERROR
            raise InitializationMonthError(
                failure_code, "Historical month could not be committed"
            ) from exc
        except Exception as exc:
            logger.exception(
                "Historical initialization month %s failed unexpectedly",
                snapshot_month,
            )
            raise InitializationMonthError(
                JobFailureCode.INTEGRITY_ERROR,
                f"Historical month failed ({type(exc).__name__}); see server logs",
            ) from exc

    def _predecessor_profile(self) -> SnapshotProfileV1 | None:
        """Resolve and gate the predecessor data version, once per run (gh-468).

        Returns ``None`` when Update is unavailable: no discoverable
        predecessor, a rebuild-forcing policy difference (detector versions,
        ingestion version, calendar dataset, request contract), or a
        predecessor without committed months to adopt from.
        """
        if self._predecessor_resolved:
            return self._predecessor
        self._predecessor_resolved = True
        try:
            previous = self._backtest_repository.previous_snapshot_profile(
                self._profile.profile_hash
            )
        except BacktestIntegrityError as exc:
            logger.warning("Predecessor data version unreadable (%s); rebuilding", exc)
            return None
        if previous is None:
            return None
        failures = adoption_gate_failures(previous, self._profile)
        if failures:
            logger.info(
                "Update unavailable because %s; rebuilding every member",
                "; ".join(failures),
            )
            return None
        if not self._backtest_repository.profile_has_committed_months(
            previous.profile_hash
        ):
            return None
        self._predecessor = previous
        return previous

    def _adopt_month(
        self,
        snapshot_month: str,
        sessions: dict[str, date],
        now: datetime,
    ) -> tuple[str | None, tuple[ResolvedSnapshotMember, ...] | None]:
        """Adopt one closed predecessor month's unchanged members (gh-468).

        Members whose stored predecessor record is a valid scan with an
        identical identity are re-derived under the new profile via
        :meth:`HistoricalScanReconstructor.adopted_record` (detector payloads
        carried, provenance recomputed); everything else -- added or changed
        members, members removed from the predecessor, and carried exclusion
        proofs, which embed alias/calendar inputs -- resolves through the
        deterministic fresh path. Returns ``(None, None)`` when this month
        cannot be adopted and the caller must resolve every member fresh.
        """
        predecessor = self._predecessor_profile()
        if predecessor is None:
            return None, None
        write_set = self._backtest_repository.snapshot_month_write_set(
            predecessor.profile_hash, snapshot_month
        )
        if write_set is None:
            return None, None
        previous_members, previous_records = write_set
        records_by_id = {
            record.security_id: record for record in previous_records
        }
        # Only valid-scan members have records; zip would mispair exclusions.
        carried = {
            member.security_id: (member, records_by_id[member.security_id])
            for member in previous_members
            if member.resolution == "valid_scan"
            and member.security_id in records_by_id
        }
        resolved: list[ResolvedSnapshotMember] = []
        adopted: list[tuple[ResolvedSnapshotMember, ReconstructionRequestV1]] = []
        for member in sorted(self._roster.members, key=lambda item: item.security_id):
            target_session = sessions[member.mic]
            previous = carried.get(member.security_id)
            adoptable = previous is not None and self._carried_identity_matches(
                previous[0], previous[1], member, target_session, snapshot_month
            )
            if adoptable:
                # The carried payloads are only valid for the evidence they
                # were computed from: if the price store now resolves a
                # different revision (re-ingestion/correction), the member
                # must resolve fresh to stay byte-identical to a Rebuild.
                request = self._evidence_request(
                    member, snapshot_month, target_session, now
                )
                evidence = cast(
                    StoredHistoricalEvidence, self._evidence_for(member, request)
                )
                adoptable = (
                    evidence.data_revision == previous[0].provider_data_revision
                )
            if adoptable:
                item, request = self._adopt_valid_member(
                    member, previous[1], snapshot_month, target_session, now
                )
                adopted.append((item, request))
            else:
                item = self._resolve_member(member, snapshot_month, target_session, now)
            resolved.append(item)
        # Bounded determinism self-check: recomputing an adopted record from
        # the same pinned evidence through the full reconstruction path must
        # reproduce it byte-for-byte; any divergence means the adoption
        # rewrite is wrong and the run must not commit partial truth.
        for item, request in self._self_check_sample(adopted):
            recomputed = self._reconstructor.reconstruct(request)
            if recomputed.record != item.record:
                raise InitializationMonthError(
                    JobFailureCode.INTEGRITY_ERROR,
                    f"Adopted record for {item.member.security_id!r} in "
                    f"{snapshot_month} diverges from its reconstruction",
                )
        if not adopted:
            # Nothing was carried: stamping predecessor provenance on a
            # 100%-fresh month would mislead adoption audits (gh-468).
            return None, tuple(resolved)
        return predecessor.profile_hash, tuple(resolved)

    @staticmethod
    def _self_check_sample(
        adopted: list[tuple[ResolvedSnapshotMember, ReconstructionRequestV1]],
    ) -> list[tuple[ResolvedSnapshotMember, ReconstructionRequestV1]]:
        """Bound the self-check to the first and last adopted member."""
        if not adopted:
            return []
        if len(adopted) == 1:
            return adopted[:1]
        return [adopted[0], adopted[-1]]

    def _carried_identity_matches(
        self,
        previous_member: SnapshotMemberV1,
        previous_record: HistoricalScanRecordV1,
        member: CapturedRosterMemberV1,
        target_session: date,
        snapshot_month: str,
    ) -> bool:
        """Whether one predecessor member may be adopted unchanged (gh-468).

        Identity, exchange, currency, quote unit, and the canonical
        month-end session must all agree; anything else resolves fresh.
        """
        return (
            previous_member.resolution == "valid_scan"
            and previous_member.mic == member.mic
            and previous_member.observed_symbol == member.provider_symbol
            and previous_member.as_of_session_date == target_session
            and previous_record.security_id == member.security_id
            and previous_record.observed_symbol == member.provider_symbol
            and previous_record.mic == member.mic
            and previous_record.snapshot_month == snapshot_month
            and previous_record.as_of_session_date == target_session
            and previous_record.currency == member.currency
            and previous_record.quote_unit == member.quote_unit
        )

    def _adopt_valid_member(
        self,
        member: CapturedRosterMemberV1,
        previous_record: HistoricalScanRecordV1,
        snapshot_month: str,
        target_session: date,
        now: datetime,
    ) -> tuple[ResolvedSnapshotMember, ReconstructionRequestV1]:
        """Re-derive one unchanged member's record under the new profile.

        The detector payloads carried from the predecessor record are pure
        functions of the member's own pinned evidence, so
        :meth:`HistoricalScanReconstructor.adopted_record` reproduces
        byte-for-byte what a fresh ``reconstruct`` would produce under the
        new input manifest (gh-468).
        """
        request = self._evidence_request(member, snapshot_month, target_session, now)
        evidence = cast(
            StoredHistoricalEvidence,
            self._evidence_for(member, request),
        )
        reconstruction_request = ReconstructionRequestV1(
            security_id=member.security_id,
            observed_symbol=evidence.observed_symbol,
            mic=member.mic,
            snapshot_month=snapshot_month,
            as_of_session_date=target_session,
            identity_candidates=(member.security_id,),
            roster=self._roster,
            evidence=evidence,
            input_manifest=self._input_manifest(
                member, snapshot_month, target_session, evidence
            ),
        )
        record = self._reconstructor.adopted_record(
            reconstruction_request,
            technicals=previous_record.technicals,
            stage=previous_record.stage,
            vcp=previous_record.vcp,
        )
        return (
            ResolvedSnapshotMember(SnapshotMemberV1.valid_scan(record), record),
            reconstruction_request,
        )

    def _evidence_request(
        self,
        member: CapturedRosterMemberV1,
        snapshot_month: str,
        target_session: date,
        now: datetime,
    ) -> HistoricalEvidenceRequest:
        """Build the canonical full-history evidence request for one member."""
        end_exclusive = date(now.year, now.month, 1)
        expected_sessions = self._calendar.sessions_in_range(
            member.mic, FULL_HISTORY_START, end_exclusive
        )
        return HistoricalEvidenceRequest(
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
            canonical_exchange_sessions=True,
        )

    def _resolve_member(
        self,
        member: CapturedRosterMemberV1,
        snapshot_month: str,
        target_session: date,
        now: datetime,
    ) -> ResolvedSnapshotMember:
        request = self._evidence_request(member, snapshot_month, target_session, now)
        try:
            evidence = self._evidence_for(member, request)
        except ProviderFailure as exc:
            raise InitializationMonthError(
                JobFailureCode(exc.code.value),
                f"{str(exc)} for {member.provider_symbol}",
            ) from exc
        first_observed = date.fromisoformat(str(evidence.rows[0]["session"]))
        observed_to_target = tuple(
            date.fromisoformat(str(row["session"]))
            for row in evidence.rows
            if date.fromisoformat(str(row["session"])) <= target_session
        )
        required_window = self._calendar.sessions_in_range(
            member.mic, FULL_HISTORY_START, target_session + timedelta(days=1)
        )[-252:]

        if (
            target_session < first_observed
            or tuple(
                session for session in observed_to_target if session in required_window
            )
            != required_window
        ):
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
            if target_session < first_observed:
                proof_builder = build_before_first_provider_observation
            else:
                observed_lifetime = self._calendar.sessions_in_range(
                    member.mic, first_observed, target_session + timedelta(days=1)
                )
                proof_builder = (
                    build_insufficient_detector_history
                    if observed_to_target == observed_lifetime
                    and len(observed_to_target) < 252
                    else build_incomplete_detector_history
                )
            proof = proof_builder(
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
        cache_key = (
            member.security_id,
            member.provider_symbol,
            request.alias_revision,
            request.start.isoformat(),
            request.end.isoformat(),
        )
        cached = self._evidence_cache.get(cache_key)
        if cached is not None:
            self._validate_cached_evidence(cached, request)
            return cached
        evidence = self._price_repository.find_request(
            security_id=member.security_id,
            requested_symbol=member.provider_symbol,
            alias_revision=self._alias_revision,
            start=FULL_HISTORY_START.isoformat(),
            end=request.end.isoformat(),
            request_contract_version=REQUEST_CONTRACT_VERSION,
            observation_policy=CANONICAL_EXCHANGE_SESSIONS_POLICY,
        )
        if evidence is None:
            compatible = self._price_repository.find_compatible_request(
                security_id=member.security_id,
                requested_symbol=member.provider_symbol,
                start=FULL_HISTORY_START.isoformat(),
                end=request.end.isoformat(),
                request_contract_version=REQUEST_CONTRACT_VERSION,
                observation_policy=CANONICAL_EXCHANGE_SESSIONS_POLICY,
            )
            if compatible is None:
                payload = self._evidence_adapter.fetch(request)
            else:
                acquired = self._price_repository.acquisition_times(
                    compatible.data_revision
                )
                if not acquired:
                    raise InitializationMonthError(
                        JobFailureCode.INTEGRITY_ERROR,
                        "Historical evidence acquisition audit is missing",
                    )
                payload = rebind_historical_evidence_alias(
                    compatible,
                    alias_revision=self._alias_revision,
                    acquired_at=acquired[0],
                )
            revision = self._price_repository.commit(payload)
            evidence = self._price_repository.verify(revision)
        self._validate_cached_evidence(evidence, request)
        self._evidence_cache[cache_key] = evidence
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
        sessions_match = bool(sessions) and (
            set(sessions).issubset(expected)
            if request.canonical_exchange_sessions
            else sessions == expected[-len(sessions) :]
        )
        if not sessions_match:
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
        lease: WorkerLeaseFenceV1 | None = None,
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
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1: ...

    def cancel_claimed_strategy_job(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1: ...

    def complete_claimed_initialization_job(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        lease: WorkerLeaseFenceV1 | None = None,
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
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> None:
        self._repository = repository
        self._month_processor = month_processor
        self._qualification_check = qualification_check
        self._profile_check = profile_check
        self._lease = lease

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
                    job_id,
                    claim_token,
                    expected_version=job.status_version,
                    lease=self._lease,
                )
            try:
                job = self._repository.set_strategy_job_current_month(
                    job_id,
                    claim_token,
                    expected_version=job.status_version,
                    month=month,
                    lease=self._lease,
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
                        lease=self._lease,
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
                    job_id,
                    claim_token,
                    expected_version=job.status_version,
                    lease=self._lease,
                )

        job = self._repository.strategy_job(job_id)
        if not self._owns(job, claim_token):
            return job
        if job.cancel_requested_at is not None:
            return self._repository.cancel_claimed_strategy_job(
                job_id,
                claim_token,
                expected_version=job.status_version,
                lease=self._lease,
            )
        try:
            return self._repository.complete_claimed_initialization_job(
                job_id,
                claim_token,
                expected_version=job.status_version,
                lease=self._lease,
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
            lease=self._lease,
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
                    job.id,
                    claim_token,
                    expected_version=job.status_version,
                    lease=self._lease,
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
                    lease=self._lease,
                )
            return current

    @staticmethod
    def _owns(job, claim_token: str) -> bool:
        return (
            job.status in {StrategyJobStatus.RUNNING, "running"}
            and job.claim_token == claim_token
        )


__all__ = ["HistoricalInitializationEngine", "InitializationMonthError"]
