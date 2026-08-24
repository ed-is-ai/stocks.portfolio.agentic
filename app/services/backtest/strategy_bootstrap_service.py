"""Bootstrap orchestration for Strategy Manager setup.

Runs the three-stage Bootstrap lifecycle (qualification → roster
capture → profile activation) through the existing repository,
qualification, and roster services. The worker process calls the
stage methods; the route layer calls ``is_setup_required()`` and
``start_setup()``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

import requests

from app.repositories.backtest_repo import BacktestIntegrityError
from app.services.backtest.strategy_job import (
    BootstrapSubmissionV1,
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobV1,
    WorkerLeaseFenceV1,
)
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_data_qualification import (
    MANDATORY_PROBE_IDS,
    HistoricalQualificationPayload,
    ProbeDefinition,
    QualificationAvailabilityService,
    QualificationRunner,
)
from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
from app.services.backtest.reconstruction_roster import (
    DataHubRosterSourceAdapter,
    MarketIdentityEvidence,
    ReconstructionRosterCaptureService,
    RosterSource,
    RosterSourcePayloadV1,
    TradingViewRosterSourceAdapter,
    YFinanceMarketIdentityResolver,
)
from app.services.backtest.security_identity import SecurityAliasManifestV1
from app.services.backtest.snapshot_profile import ProfileDetectorV1, SnapshotProfileV1
from app.services.backtest.source_manifest import (
    detector_source_manifests,
    yfinance_ingestion_source_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar

if TYPE_CHECKING:
    from app.repositories.backtest_repo import BacktestRepository
    from app.services.backtest.strategy_job_service import StrategyJobService


def _is_fixture_environment() -> bool:
    """Return True only when Fixture composition was explicitly requested."""
    return bool(os.environ.get("STRATEGY_FIXTURE"))


class StrategyBootstrapService:
    """Orchestrate the Bootstrap setup lifecycle.

    The service is the one entry point for setup: the route layer calls
    ``is_setup_required()`` / ``start_setup()``, and the worker process
    calls the stage methods (``_run_qualification``, ``_capture_roster``,
    ``_activate_profile``) through the existing ``StageWalkEngine``
    scaffold.
    """

    def __init__(
        self,
        repository: "BacktestRepository",
        jobs: "StrategyJobService | None",
        *,
        clock: "datetime | None" = None,
        providers: "StrategyProviderBundleV1 | None" = None,
    ) -> None:
        self._repository = repository
        self._jobs = jobs
        self._clock = clock
        self._providers = providers or StrategyProviderBundleV1.for_environment(
            repository
        )
        self._qualification_contract_digest: str | None = None
        self._captured_roster_digest: str | None = None

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock
        return datetime.now(timezone.utc)

    def is_setup_required(self) -> bool:
        """Return True when no active profile exists."""
        return self._repository.active_snapshot_profile() is None

    def is_already_set_up(self) -> tuple[bool, datetime | None]:
        """Return ``(True, activated_at)`` if a compatible profile exists.

        A compatible repeat is a verified no-op: the setup action returns
        without enqueuing a new bootstrap job.
        """
        active = self._repository.active_snapshot_profile()
        if active is None:
            return False, None
        return True, active.activated_at

    def start_setup(self, submission: BootstrapSubmissionV1) -> StrategyJobV1:
        """Enqueue one bootstrap job.

        If a compatible active profile already exists, this is a no-op
        and raises :class:`StrategyBootstrapAlreadySetUp`.
        """
        if self._jobs is None:
            raise RuntimeError("no job service configured")
        result = self._jobs.enqueue_bootstrap(submission)
        if result.no_op:
            raise StrategyBootstrapAlreadySetUp("Strategy Manager is already set up")
        if result.job is None:
            raise RuntimeError("accepted Bootstrap submission did not return a job")
        return result.job

    # ------------------------------------------------------------------
    # Stage methods -- called by the worker's StageWalkEngine
    # ------------------------------------------------------------------

    def _run_qualification(self) -> None:
        """Stage 1: run and verify the pinned qualification contract."""
        try:
            contract = self._providers.qualification_runner.contract()
        except Exception as exc:
            raise _bootstrap_failure(exc, JobFailureCode.PROVIDER_CONTRACT_ERROR) from exc
        available = QualificationAvailabilityService(self._repository).availability(
            contract
        )
        if available.available:
            self._qualification_contract_digest = contract.contract_digest
            return
        try:
            contract = self._providers.qualification_runner.run()
            available = QualificationAvailabilityService(self._repository).availability(
                contract
            )
        except Exception as exc:
            raise _bootstrap_failure(exc, JobFailureCode.PROVIDER_CONTRACT_ERROR) from exc
        if not available.available:
            latest = self._repository.latest_qualification(contract.contract_digest)
            code = (
                JobFailureCode.PROVIDER_UNAVAILABLE
                if latest is None or latest.failure_code is None
                else _job_failure_code(
                    latest.failure_code, JobFailureCode.PROVIDER_UNAVAILABLE
                )
            )
            detail = (
                "Historical data qualification is not available"
                if latest is None or not latest.failure_reason
                else latest.failure_reason
            )
            raise BootstrapStageFailure(
                code, detail
            )
        self._qualification_contract_digest = contract.contract_digest

    def _capture_roster(
        self,
        lineage_id: str,
        claim_token: str | None = None,
        *,
        expected_version: int | None = None,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> None:
        """Stage 2: capture the complete required-source roster evidence."""
        if self._repository.active_snapshot_profile() is not None:
            return
        try:
            captured = self._providers.roster_capture.capture(
                lineage_id,
                self._providers.alias_manifest,
                job_claim=(
                    None
                    if claim_token is None or expected_version is None
                    else (lineage_id, claim_token, expected_version)
                ),
                lease=lease,
            )
        except StrategyJobConflict:
            raise
        except Exception as exc:
            raise _bootstrap_failure(exc, JobFailureCode.REQUIRED_DATA_MISSING) from exc
        self._captured_roster_digest = captured.roster_digest

    def _activate_profile(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1:
        """Stage 3: atomically persist/activate the real profile and complete."""
        active = self._repository.active_snapshot_profile()
        if active is not None:
            try:
                active_profile = self._repository.snapshot_profile(active.profile_hash)
                if active_profile is None:
                    raise BacktestIntegrityError("active snapshot profile is missing")
                self._repository.validate_bau_profile_authority(active_profile)
            except Exception as exc:
                raise _bootstrap_failure(exc, JobFailureCode.INTEGRITY_ERROR) from exc
            if (
                self._captured_roster_digest is not None
                and active_profile.roster_digest != self._captured_roster_digest
            ):
                raise BootstrapStageFailure(
                    JobFailureCode.INTEGRITY_ERROR,
                    "Active profile does not match captured Bootstrap evidence",
                )
            return self._repository.complete_claimed_stage_job(
                job_id, claim_token, expected_version=expected_version, lease=lease
            )
        else:
            if self._captured_roster_digest is None:
                raise BootstrapStageFailure(
                    JobFailureCode.REQUIRED_DATA_MISSING,
                    "No reconstruction roster is available",
                )
            if self._qualification_contract_digest is None:
                raise BootstrapStageFailure(
                    JobFailureCode.REQUIRED_DATA_MISSING,
                    "No qualified historical contract is available",
                )
            try:
                profile = self._providers.snapshot_profile(self._captured_roster_digest)
            except Exception as exc:
                raise _bootstrap_failure(exc, JobFailureCode.INTEGRITY_ERROR) from exc
        try:
            return self._repository.activate_bootstrap_profile_and_complete(
                profile,
                job_id,
                claim_token,
                expected_version=expected_version,
                qualification_contract_digest=self._qualification_contract_digest,
                lease=lease,
            )
        except (BacktestIntegrityError, StrategyJobConflict) as exc:
            raise _bootstrap_failure(exc, JobFailureCode.INTEGRITY_ERROR) from exc

    @property
    def is_fixture(self) -> bool:
        """Return whether the selected provider bundle is explicitly Fixture."""
        return self._providers.mode == "fixture"


@dataclass(frozen=True)
class StrategyProviderBundleV1:
    """Explicit production/fixture Bootstrap evidence composition."""

    qualification_runner: QualificationRunner
    roster_capture: ReconstructionRosterCaptureService
    alias_manifest: SecurityAliasManifestV1
    snapshot_profile: Callable[[str], SnapshotProfileV1]
    mode: Literal["production", "fixture"] = "production"

    @classmethod
    def for_environment(
        cls, repository: "BacktestRepository"
    ) -> "StrategyProviderBundleV1":
        if _is_fixture_environment():
            return cls.fixture(repository)
        return cls.production(repository)

    @classmethod
    def production(cls, repository: "BacktestRepository") -> "StrategyProviderBundleV1":
        now = datetime.now(timezone.utc)
        calendar = TradingCalendar()
        fixture_path = _qualification_fixture_path()
        probes = _production_probes()
        runner = QualificationRunner(repository, fixture_path, probes, calendar=calendar)
        roster = ReconstructionRosterCaptureService(
            repository,
            (
                DataHubRosterSourceAdapter(_fetch_datahub_sp500),
                TradingViewRosterSourceAdapter("US"),
                TradingViewRosterSourceAdapter("UK"),
            ),
            YFinanceMarketIdentityResolver(),
            policy=None,
        )
        aliases = SecurityAliasManifestV1.build((), created_at=now)

        def profile(roster_digest: str) -> SnapshotProfileV1:
            manifests = detector_source_manifests(Path(__file__).resolve().parents[3])
            return SnapshotProfileV1(
                schema_version="snapshot_profile.v1",
                display_version="Scanner data v1",
                record_schema_version="historical_scan_record.v1",
                detectors=tuple(ProfileDetectorV1(detector_id=item.detector_id, detector_api_version=item.detector_api_version, detector_version=manifests[item.detector_id].digest) for item in DETECTOR_REGISTRY),
                roster_policy_version="ReconstructionRosterPolicyV1",
                roster_digest=roster_digest,
                identity_registry_version="SecurityIdentityRegistryV1",
                alias_policy_version="SecurityAliasManifestV1",
                source_policy_version="FreeHistoricalSourcePolicyV1",
                calendar_policy_version="PerExchangeMonthEndV1",
                calendar_dataset_version="exchange-calendars-v1",
                calendar_dataset_digest=calendar.session_table_digest(),
                yfinance_request_contract_version="YFinanceDailyProviderNativeV1",
                yfinance_ingestion_version=yfinance_ingestion_source_manifest(Path(__file__).resolve().parents[3]).digest,
                market_plane_policy_version=PRICE_VOLUME_PLANE_VERSION,
                reconstructability_policy_version="reconstructability.v1",
                provenance_vocabulary=("best_effort_reconstructed", "observed_bau"),
                cadence="per-exchange month_end",
            )

        return cls(runner, roster, aliases, profile, "production")

    @classmethod
    def fixture(cls, repository: "BacktestRepository") -> "StrategyProviderBundleV1":
        """Build the explicit, deterministic non-production composition."""
        fixed = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        fixture_path = _qualification_fixture_path()
        probes = _production_probes()
        runner = QualificationRunner(
            repository,
            fixture_path,
            probes,
            live_adapter=_PinnedQualificationAdapter(fixture_path),
            clock=lambda: fixed,
        )
        payloads = (
            _fixture_roster_payload(
                RosterSource.DATAHUB_SP500,
                ({"symbol": "AAPL", "name": "Apple", "sector": "Technology"},),
                fixed,
            ),
            _fixture_roster_payload(
                RosterSource.TRADINGVIEW_US,
                ({"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"},),
                fixed,
            ),
            _fixture_roster_payload(
                RosterSource.TRADINGVIEW_UK,
                ({"symbol": "LSE:ULVR", "exchange": "LSE", "currency": "GBp"},),
                fixed,
            ),
        )
        identifiers = iter(
            (
                "fixture-security-0001",
                "fixture-security-0002",
            )
        )
        roster = ReconstructionRosterCaptureService(
            repository,
            (
                lambda: payloads[0],
                lambda: payloads[1],
                lambda: payloads[2],
            ),
            lambda _symbol, _row: MarketIdentityEvidence(
                "XNAS", "USD", "USD", "pinned_fixture", "1" * 64
            ),
            id_generator=lambda: next(identifiers),
            clock=lambda: fixed,
        )
        production = cls.production(repository)
        return cls(
            runner,
            roster,
            SecurityAliasManifestV1.build((), created_at=fixed),
            production.snapshot_profile,
            "fixture",
        )


def _production_probes() -> dict[str, ProbeDefinition]:
    definitions = {
        "us_active": ("AAPL", "USD", "USD", "America/New_York"),
        "lse_active": ("ULVR.L", "GBP", "GBp", "Europe/London"),
        "gbpusd": ("GBPUSD=X", "USD", "USD", "UTC"),
    }
    return {
        name: ProbeDefinition(
            symbol=definitions[name][0],
            start=date(2024, 1, 1),
            end=date(2024, 1, 4),
            expected_currency=definitions[name][1],
            expected_quote_unit=definitions[name][2],
            expected_timezone=definitions[name][3],
            expected_sessions=(date(2024, 1, 2), date(2024, 1, 3)),
            allowed_observed_symbols=(definitions[name][0],),
        )
        for name in MANDATORY_PROBE_IDS
    }


def _fetch_datahub_sp500() -> list[dict[str, object]] | None:
    response = requests.get("https://datahub.io/core/s-and-p-500-companies-financials/r/constituents-financials.csv", timeout=15)
    response.raise_for_status()
    return [
        {
            "symbol": row.get("Symbol", row.get("symbol", "")),
            "name": row.get("Name", row.get("name", "")),
            "sector": row.get("Sector", row.get("sector", "")),
        }
        for row in csv.DictReader(StringIO(response.text))
    ]


def _bootstrap_failure(exc: Exception, fallback: JobFailureCode) -> BootstrapStageFailure:
    if isinstance(exc, requests.RequestException):
        return BootstrapStageFailure(
            JobFailureCode.PROVIDER_UNAVAILABLE, "Bootstrap evidence stage failed"
        )
    code = getattr(exc, "code", fallback)
    job_code = _job_failure_code(code, fallback)
    return BootstrapStageFailure(job_code, "Bootstrap evidence stage failed")


def _job_failure_code(code: object, fallback: JobFailureCode) -> JobFailureCode:
    try:
        return JobFailureCode(str(code))
    except ValueError:
        return fallback


def _qualification_fixture_path() -> Path:
    return Path(__file__).with_name("fixtures") / "market_mechanics_v1.json"


class _PinnedQualificationAdapter:
    def __init__(self, fixture_path: Path) -> None:
        payload = json.loads(fixture_path.read_text())
        self._cases = {
            str(case["requested_symbol"]): case for case in payload["provider_cases"]
        }

    def fetch(self, definition: ProbeDefinition) -> HistoricalQualificationPayload:
        case = self._cases[definition.symbol]
        return HistoricalQualificationPayload(
            requested_symbol=definition.symbol,
            observed_symbol=definition.allowed_observed_symbols[0],
            currency=definition.expected_currency,
            quote_unit=definition.expected_quote_unit,
            quote_unit_scale="0.01" if definition.expected_quote_unit == "GBp" else "1",
            exchange_timezone=definition.expected_timezone,
            request_contract={"fixture": True},
            rows=(),
            response_metadata_digest=str(case["expected_content_digest"]),
            content_digest=str(case["expected_content_digest"]),
            acquired_at="2026-08-10T12:00:00+00:00",
        )


def _fixture_roster_payload(
    source: RosterSource,
    rows: tuple[dict[str, object], ...],
    retrieved_at: datetime,
) -> RosterSourcePayloadV1:
    return RosterSourcePayloadV1.build(
        source=source,
        rows=rows,
        retrieved_at=retrieved_at,
        source_version="pinned-bootstrap-fixture-v1",
        package_version="fixture",
        config_version="ReconstructionRosterPolicyV1",
    )


class BootstrapStageFailure(Exception):
    """One typed Bootstrap stage failure with a stable failure code."""

    def __init__(self, code: JobFailureCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class StrategyBootstrapAlreadySetUp(Exception):
    """Setup was requested but a compatible profile is already active."""


__all__ = [
    "BootstrapStageFailure",
    "StrategyBootstrapAlreadySetUp",
    "StrategyProviderBundleV1",
    "StrategyBootstrapService",
]
