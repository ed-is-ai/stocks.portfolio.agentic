"""Deterministic month-boundary orchestration for historical initialization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from time import monotonic, sleep
from pathlib import Path
from typing import Protocol, cast

from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.db import sqlite_failure_detail
from app.repositories.historical_price_repo import (
    HistoricalPriceRepository,
    StoredHistoricalEvidence,
)
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_data_qualification import (
    REQUEST_CONTRACT_VERSION,
    FailureCode,
    ProviderFailure,
)
from app.services.backtest.historical_price_evidence import (
    CANONICAL_EXCHANGE_SESSIONS_POLICY,
    HistoricalEvidencePayload,
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
    record_composition_source_manifest,
    yfinance_ingestion_source_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.trading_calendar import CalendarContractError
from app.services.backtest.wiki_historical_evidence import (
    WIKI_PROVIDER,
    WIKI_REQUEST_CONTRACT_VERSION,
)

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

#: Extra waits (seconds) before re-asking the provider after a retryable
#: ``provider_unavailable`` that survived the adapter's own quick retries.
PROVIDER_RETRY_WAITS_SECONDS: tuple[float, ...] = (30.0, 120.0)

#: Price provider of a roster member that names none. Every member is
#: yfinance today; #82 C assigns ``wiki`` to members only WIKI prices.
DEFAULT_PROVIDER = "yfinance"

#: Request contract each provider's evidence is stored under.
_REQUEST_CONTRACT_VERSIONS = {
    DEFAULT_PROVIDER: REQUEST_CONTRACT_VERSION,
    WIKI_PROVIDER: WIKI_REQUEST_CONTRACT_VERSION,
}


class EvidenceAdapter(Protocol):
    """Fetch one provider-native evidence interval."""

    def fetch(
        self, definition: HistoricalEvidenceRequest
    ) -> HistoricalEvidencePayload: ...


def member_provider(member: CapturedRosterMemberV1) -> str:
    """Return the price provider a roster member names, else yfinance."""
    return getattr(member, "provider", None) or DEFAULT_PROVIDER


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


type EvidenceCacheKey = tuple[str, str, str | None, str, str]


@dataclass(frozen=True)
class InitializationMonthOutcome:
    reused_securities: int
    fetched_securities: int


class CanonicalSnapshotMonthProcessor:
    """Compose existing evidence/reconstruction APIs into one Ready month."""

    _TIMEZONES = {
        "BATS": "America/New_York",
        "XNAS": "America/New_York",
        "XNYS": "America/New_York",
        "XLON": "Europe/London",
    }

    _DETECTOR_WORKERS = max(4, min(os.cpu_count() or 4, 8))

    #: Evidence end pinned once per run (first day of the run-start month),
    #: so a run crossing a month boundary never switches end mid-run.
    _run_end: date | None = None

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
        evidence_adapters: Mapping[str, EvidenceAdapter] | None = None,
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
        # Adapters for non-default providers (e.g. ``wiki``), by provider.
        self._evidence_adapters = dict(evidence_adapters or {})
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
        # full-history evidence window. Retain both the verified evidence and
        # its successful validation, so later months do not reparse its rows.
        self._evidence_cache: dict[EvidenceCacheKey, StoredHistoricalEvidence] = {}
        self._validated_evidence_cache: set[EvidenceCacheKey] = set()
        self._fetched_security_ids: set[str] = set()
        try:
            roster_payload = json.loads(roster.canonical_manifest_json)
            self._alias_revision = str(roster_payload["alias_revision"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("reconstruction roster alias evidence is invalid") from exc

    def __call__(self, snapshot_month: str) -> InitializationMonthOutcome:
        self._fetched_security_ids = set()
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
                resolved = self._resolve_fresh_members(
                    sorted(self._roster.members, key=lambda item: item.security_id),
                    snapshot_month,
                    sessions,
                    now,
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
            fetched = len(self._fetched_security_ids)
            return InitializationMonthOutcome(
                len(self._roster.members) - fetched, fetched
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
        records_by_id = {record.security_id: record for record in previous_records}
        # Only valid-scan members have records; zip would mispair exclusions.
        carried = {
            member.security_id: (member, records_by_id[member.security_id])
            for member in previous_members
            if member.resolution == "valid_scan" and member.security_id in records_by_id
        }
        resolved: dict[str, ResolvedSnapshotMember] = {}
        adopted: list[tuple[ResolvedSnapshotMember, ReconstructionRequestV1]] = []
        fresh_requests: list[ReconstructionRequestV1] = []
        for member in sorted(self._roster.members, key=lambda item: item.security_id):
            target_session = sessions[member.mic]
            previous = carried.get(member.security_id)
            adoptable = previous is not None and self._carried_identity_matches(
                previous[0], previous[1], member, target_session, snapshot_month
            )
            if adoptable:
                # ``adoptable`` is True only when the earlier short-circuit
                # proved ``previous is not None`` -- assert it so the type
                # checker can see the same invariant.
                assert previous is not None
                # The carried payloads are only valid for the evidence they
                # were computed from: if the price store now resolves a
                # different revision (re-ingestion/correction), the member
                # must resolve fresh to stay byte-identical to a Rebuild.
                request = self._evidence_request(member, self._pinned_end(now))
                evidence = cast(
                    StoredHistoricalEvidence,
                    self._evidence_for(member, request, target_session),
                )
                adoptable = evidence.data_revision == previous[0].provider_data_revision
            if adoptable:
                assert previous is not None
                item, request = self._adopt_valid_member(
                    member, previous[1], snapshot_month, target_session, now
                )
                adopted.append((item, request))
            else:
                prepared = self._prepare_member(
                    member, snapshot_month, target_session, now
                )
                if isinstance(prepared, ReconstructionRequestV1):
                    fresh_requests.append(prepared)
                    continue
                item = prepared
            resolved[member.security_id] = item
        for item in self._reconstruct_requests(fresh_requests):
            resolved[item.member.security_id] = item
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
            return None, tuple(
                resolved[member.security_id]
                for member in sorted(
                    self._roster.members, key=lambda item: item.security_id
                )
            )
        return predecessor.profile_hash, tuple(
            resolved[member.security_id]
            for member in sorted(
                self._roster.members, key=lambda item: item.security_id
            )
        )

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
        request = self._evidence_request(member, self._pinned_end(now))
        evidence = cast(
            StoredHistoricalEvidence,
            self._evidence_for(member, request, target_session),
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

    def _pinned_end(self, now: datetime) -> date:
        """Pin the run's evidence end on first use and keep it for the run."""
        if self._run_end is None:
            self._run_end = date(now.year, now.month, 1)
            logger.info(
                "Historical initialization evidence end pinned to %s", self._run_end
            )
        return self._run_end

    def _evidence_request(
        self, member: CapturedRosterMemberV1, end_exclusive: date
    ) -> HistoricalEvidenceRequest:
        """Build the canonical full-history evidence request for one member."""
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
        prepared = self._prepare_member(member, snapshot_month, target_session, now)
        if not isinstance(prepared, ReconstructionRequestV1):
            return prepared
        return self._reconstruct_requests((prepared,))[0]

    def _resolve_fresh_members(
        self,
        members: list[CapturedRosterMemberV1],
        snapshot_month: str,
        sessions: dict[str, date],
        now: datetime,
    ) -> tuple[ResolvedSnapshotMember, ...]:
        resolved: dict[str, ResolvedSnapshotMember] = {}
        requests: list[ReconstructionRequestV1] = []
        for member in members:
            prepared = self._prepare_member(
                member, snapshot_month, sessions[member.mic], now
            )
            if isinstance(prepared, ReconstructionRequestV1):
                requests.append(prepared)
            else:
                resolved[member.security_id] = prepared
        for item in self._reconstruct_requests(requests):
            resolved[item.member.security_id] = item
        return tuple(resolved[member.security_id] for member in members)

    def _reconstruct_requests(
        self,
        requests: tuple[ReconstructionRequestV1, ...] | list[ReconstructionRequestV1],
    ) -> tuple[ResolvedSnapshotMember, ...]:
        if not requests:
            return ()
        results = self._reconstructor.reconstruct_many(
            requests,
            parallel_workers=self._DETECTOR_WORKERS,
        )
        return tuple(
            ResolvedSnapshotMember(
                SnapshotMemberV1.valid_scan(result.record), result.record
            )
            for result in results
        )

    def _prepare_member(
        self,
        member: CapturedRosterMemberV1,
        snapshot_month: str,
        target_session: date,
        now: datetime,
    ) -> ResolvedSnapshotMember | ReconstructionRequestV1:
        request = self._evidence_request(member, self._pinned_end(now))
        try:
            evidence = self._evidence_for(member, request, target_session)
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

        return ReconstructionRequestV1(
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

    def _evidence_for(
        self,
        member: CapturedRosterMemberV1,
        request: HistoricalEvidenceRequest,
        target_session: date | None = None,
    ) -> StoredHistoricalEvidence:
        """Resolve one member's evidence: cache, stored revision, then provider.

        A stored revision ending at the previous month start is reused when
        none ends at ``request.end`` and it still covers ``target_session``;
        otherwise the provider is asked with ``request.end``.
        """
        candidates = [request]
        previous_end = (request.end - timedelta(days=1)).replace(day=1)
        if target_session is not None and target_session < previous_end:
            candidates.append(self._evidence_request(member, previous_end))
        for candidate in candidates:
            cache_key = self._cache_key(member, candidate)
            cached = self._evidence_cache.get(cache_key)
            if cached is not None:
                if cache_key not in self._validated_evidence_cache:
                    self._validate_cached_evidence(cached, candidate)
                    self._validated_evidence_cache.add(cache_key)
                return cached
        evidence: StoredHistoricalEvidence | None = None
        for candidate in candidates:
            evidence = self._stored_evidence(member, candidate)
            if evidence is not None:
                request = candidate
                break
        if evidence is None:
            payload = self._fetch_with_retry(request, member_provider(member))
            self._fetched_security_ids.add(member.security_id)
            revision = self._price_repository.commit(payload)
            evidence = self._price_repository.verify(revision)
        cache_key = self._cache_key(member, request)
        self._validate_cached_evidence(evidence, request)
        self._evidence_cache[cache_key] = evidence
        self._validated_evidence_cache.add(cache_key)
        return evidence

    @staticmethod
    def _cache_key(
        member: CapturedRosterMemberV1, request: HistoricalEvidenceRequest
    ) -> EvidenceCacheKey:
        return (
            member.security_id,
            member.provider_symbol,
            request.alias_revision,
            request.start.isoformat(),
            request.end.isoformat(),
        )

    def _stored_evidence(
        self, member: CapturedRosterMemberV1, request: HistoricalEvidenceRequest
    ) -> StoredHistoricalEvidence | None:
        """Return a stored revision for exactly ``request.end``, if any."""
        contract_version = _REQUEST_CONTRACT_VERSIONS.get(member_provider(member))
        if contract_version is None:
            return None
        evidence = self._price_repository.find_request(
            security_id=member.security_id,
            requested_symbol=member.provider_symbol,
            alias_revision=self._alias_revision,
            start=FULL_HISTORY_START.isoformat(),
            end=request.end.isoformat(),
            request_contract_version=contract_version,
            observation_policy=CANONICAL_EXCHANGE_SESSIONS_POLICY,
        )
        if evidence is not None:
            return evidence
        compatible = self._price_repository.find_compatible_request(
            security_id=member.security_id,
            requested_symbol=member.provider_symbol,
            start=FULL_HISTORY_START.isoformat(),
            end=request.end.isoformat(),
            request_contract_version=contract_version,
            observation_policy=CANONICAL_EXCHANGE_SESSIONS_POLICY,
        )
        if compatible is None:
            return None
        acquired = self._price_repository.acquisition_times(compatible.data_revision)
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
        return self._price_repository.verify(revision)

    def _adapter_for(self, provider: str) -> EvidenceAdapter:
        """Return the evidence adapter for ``provider``."""
        if provider == DEFAULT_PROVIDER:
            return self._evidence_adapter
        adapter = self._evidence_adapters.get(provider)
        if adapter is None:
            raise ProviderFailure(
                FailureCode.PROVIDER_CONTRACT_ERROR,
                f"No historical evidence adapter for provider {provider!r}",
            )
        return adapter

    def _fetch_with_retry(
        self, request: HistoricalEvidenceRequest, provider: str = DEFAULT_PROVIDER
    ) -> HistoricalEvidencePayload:
        """Fetch, re-trying a retryable ``provider_unavailable`` after waits."""
        adapter = self._adapter_for(provider)
        for wait in (*PROVIDER_RETRY_WAITS_SECONDS, None):
            try:
                return adapter.fetch(request)
            except ProviderFailure as exc:
                transient = exc.retryable and (
                    exc.code is FailureCode.PROVIDER_UNAVAILABLE
                )
                if not transient or wait is None:
                    raise
                logger.warning(
                    "Provider unavailable for %s; retrying in %ss",
                    request.symbol,
                    wait,
                )
                sleep(wait)
        raise AssertionError("unreachable")

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
            record_composition_version=record_composition_source_manifest(
                self._project_root
            ).digest,
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

    def record_initialization_month_commit(
        self,
        job_id: str,
        claim_token: str,
        *,
        month: str,
        reused_securities: int,
        fetched_securities: int,
        fresh_elapsed_seconds: float,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> object: ...

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
        month_processor: Callable[[str], InitializationMonthOutcome | None],
        *,
        qualification_check: Callable[[], bool] = lambda: True,
        profile_check: Callable[[str], bool] = lambda _profile_hash: True,
        lease: WorkerLeaseFenceV1 | None = None,
        security_count: int = 0,
    ) -> None:
        self._repository = repository
        self._month_processor = month_processor
        self._qualification_check = qualification_check
        self._profile_check = profile_check
        self._lease = lease
        self._security_count = security_count

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
                started = monotonic()
                readiness = self._repository.interval_readiness(
                    initialization.profile_hash, month, month
                )
                outcome = (
                    InitializationMonthOutcome(self._security_count, 0)
                    if readiness.ready
                    else self._month_processor(month)
                    or InitializationMonthOutcome(0, self._security_count)
                )
                recorder = getattr(
                    self._repository, "record_initialization_month_commit", None
                )
                if recorder is not None:
                    recorder(
                        job_id,
                        claim_token,
                        month=month,
                        reused_securities=outcome.reused_securities,
                        fetched_securities=outcome.fetched_securities,
                        fresh_elapsed_seconds=(
                            monotonic() - started if outcome.fetched_securities else 0
                        ),
                        lease=self._lease,
                    )
            except InitializationMonthError as exc:
                current = self._repository.strategy_job(job_id)
                if not self._owns(current, claim_token):
                    return current
                return self._fail_or_cancel(
                    current,
                    claim_token,
                    exc.code,
                    month,
                    sqlite_failure_detail(exc, "initialization.month", exc.detail),
                )
            except Exception as exc:
                current = self._repository.strategy_job(job_id)
                if not self._owns(current, claim_token):
                    return current
                return self._fail_or_cancel(
                    current,
                    claim_token,
                    JobFailureCode.INTEGRITY_ERROR,
                    month,
                    sqlite_failure_detail(
                        exc,
                        "initialization.month",
                        "Historical initialization failed integrity validation",
                    ),
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
