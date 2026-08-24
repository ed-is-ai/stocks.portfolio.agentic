"""Tests for StrategyBootstrapService (Story 4.3).

Tests the Bootstrap setup lifecycle: setup detection, idempotent no-op,
stage execution, failure handling, and fixture environment labelling.
"""

from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest
import requests

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository, StrategyJobConflict
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_data_qualification import (
    FIXTURE_CONTRACT_VERSION,
    FailureCode,
    HistoricalQualificationPayload,
    MANDATORY_PROBE_IDS,
    ProbeDefinition,
    ProviderFailure,
    QualificationRunner,
    REQUEST_CONTRACT_VERSION,
    current_source_versions_json,
)
from app.services.backtest.reconstruction_roster import (
    MarketIdentityEvidence,
    ReconstructionRosterCaptureService,
    RosterCaptureError,
    RosterSource,
    RosterSourcePayloadV1,
)
from app.services.backtest.security_identity import SecurityAliasManifestV1
from app.services.backtest.strategy_bootstrap_service import (
    BootstrapStageFailure,
    StrategyBootstrapAlreadySetUp,
    StrategyBootstrapService,
    StrategyProviderBundleV1,
    _fetch_datahub_sp500,
    _bootstrap_failure,
    _production_probes,
)
from app.services.backtest.strategy_job import (
    BootstrapStage,
    BootstrapSubmissionV1,
    JobFailureCode,
    PrerequisiteState,
    StrategyJobStatus,
    StrategyJobType,
)
from app.services.backtest.snapshot_profile import ProfileDetectorV1
from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.strategy_readiness_service import StrategyReadinessService
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.worker import build_stage_walk_engine
import json

NOW = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)
PROFILE_HASH = "a" * 64
ROSTER_DIGEST = "b" * 64
FIXTURE_DIGEST = "1" * 64
PROBE_DEFINITION_DIGEST = "2" * 64
QUALIFICATION_FIXTURE = Path(__file__).parent / "fixtures" / "market_mechanics_v1.json"


def _qualification_digest() -> str:
    return manifest_digest(
        {
            "sources": json.loads(current_source_versions_json()),
            "calendar_digest": TradingCalendar().session_table_digest(),
            "request_contract": REQUEST_CONTRACT_VERSION,
            "fixture_contract": FIXTURE_CONTRACT_VERSION,
            "fixture_digest": FIXTURE_DIGEST,
            "probe_definition_digest": PROBE_DEFINITION_DIGEST,
        }
    )


def _repo(path: Path) -> BacktestRepository:
    repo = BacktestRepository(
        db.make_connect(lambda: str(path)),
        clock=lambda: NOW.date(),
        instant_clock=lambda: NOW,
    )
    repo.ensure_schema()
    _seed(path)
    return repo


def _seed(path: Path) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT OR IGNORE INTO security_identity_registry_revisions
               (revision_digest, canonical_manifest_json, evidence_digest,
                created_at)
               VALUES (?, '{}', ?, ?)""",
            ("c" * 64, "d" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO security_alias_manifests
               (alias_revision, canonical_manifest_json, evidence_digest,
                created_at)
               VALUES (?, '{}', ?, ?)""",
            ("e" * 64, "f" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO reconstruction_rosters
               (roster_digest, policy_version, canonical_manifest_json,
                identity_registry_revision, alias_revision, captured_at)
               VALUES (?, 'ReconstructionRosterPolicyV1', '{}', ?, ?, ?)""",
            (ROSTER_DIGEST, "c" * 64, "e" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO security_identities
               (security_id, mic, provider_symbol, evidence_digest,
                identity_registry_revision, created_at)
               VALUES (?, 'XNYS', 'TEST', ?, ?, ?)""",
            ("sid_test_001", "g" * 64, "c" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO snapshot_profiles
               (profile_hash, canonical_profile_json, display_version,
                roster_digest, scanner_schema_version,
                calendar_dataset_version, calendar_dataset_digest, cadence)
               VALUES (?, '{}', 'Scanner data v1', ?,
                       'historical_scan_record.v1', 'exchange-calendars-v1',
                       ?, 'per-exchange month_end')""",
            (
                PROFILE_HASH,
                ROSTER_DIGEST,
                TradingCalendar().session_table_digest(),
            ),
        )
        conn.execute(
            """INSERT OR IGNORE INTO active_snapshot_profile
               (singleton_id, profile_hash, activation_seq, activated_at)
               VALUES (1, ?, 1, ?)""",
            (PROFILE_HASH, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO historical_source_qualifications
               (contract_digest, source_versions_json, fixture_digest,
                probe_definition_digest, probe_digest, qualified_at,
                passed, failure_code, failure_reason)
               VALUES (?, ?, ?, ?, ?, ?, 1, NULL, NULL)""",
            (
                _qualification_digest(),
                current_source_versions_json(),
                FIXTURE_DIGEST,
                PROBE_DEFINITION_DIGEST,
                "3" * 64,
                NOW.isoformat(),
            ),
        )


def _empty_repo(path: Path) -> BacktestRepository:
    repo = BacktestRepository(
        db.make_connect(lambda: str(path)),
        clock=lambda: NOW.date(),
        instant_clock=lambda: NOW,
    )
    repo.ensure_schema()
    return repo


def _create_bootstrap_stage_job(repo: BacktestRepository):
    return repo._create_stage_job(StrategyJobType.BOOTSTRAP, None)


def _probes() -> dict[str, ProbeDefinition]:
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


class _FakeQualificationAdapter:
    def fetch(self, definition: ProbeDefinition) -> HistoricalQualificationPayload:
        return HistoricalQualificationPayload(
            requested_symbol=definition.symbol,
            observed_symbol=definition.symbol,
            currency=definition.expected_currency,
            quote_unit=definition.expected_quote_unit,
            quote_unit_scale=(
                "0.01" if definition.expected_quote_unit == "GBp" else "1"
            ),
            exchange_timezone=definition.expected_timezone,
            request_contract={},
            rows=(),
            response_metadata_digest="m" * 64,
            content_digest=(definition.symbol.encode().hex() + "0" * 64)[:64],
            acquired_at=NOW.isoformat(),
        )


class _UnavailableQualificationAdapter:
    def fetch(self, definition: ProbeDefinition) -> HistoricalQualificationPayload:
        raise ConnectionError(f"fixture provider unavailable for {definition.symbol}")


def _payload(source: RosterSource, rows: list[dict[str, object]]) -> RosterSourcePayloadV1:
    return RosterSourcePayloadV1.build(
        source=source, rows=rows, retrieved_at=NOW, source_version="test-v1",
        package_version="test", config_version="ReconstructionRosterPolicyV1",
    )


def _production_bundle(repo: BacktestRepository) -> StrategyProviderBundleV1:
    payloads = (
        _payload(
            RosterSource.DATAHUB_SP500,
            [{"symbol": "AAPL", "name": "Apple", "sector": "Technology"}],
        ),
        _payload(RosterSource.TRADINGVIEW_US, [{"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"}]),
        _payload(RosterSource.TRADINGVIEW_UK, [{"symbol": "LSE:ULVR", "exchange": "LSE", "currency": "GBp"}]),
    )
    def datahub() -> RosterSourcePayloadV1:
        return payloads[0]

    def tradingview_us() -> RosterSourcePayloadV1:
        return payloads[1]

    def tradingview_uk() -> RosterSourcePayloadV1:
        return payloads[2]

    captures = ReconstructionRosterCaptureService(
        repo,
        (datahub, tradingview_us, tradingview_uk),
        lambda _symbol, _row: MarketIdentityEvidence("XNAS", "USD", "USD", "test", "i" * 64),
        clock=lambda: NOW,
    )
    production = StrategyProviderBundleV1.production(repo)
    return StrategyProviderBundleV1(
        QualificationRunner(repo, QUALIFICATION_FIXTURE, _probes(), live_adapter=_FakeQualificationAdapter(), clock=lambda: NOW),
        captures, SecurityAliasManifestV1.build((), created_at=NOW), production.snapshot_profile,
    )


def _unavailable_bundle(repo: BacktestRepository) -> StrategyProviderBundleV1:
    bundle = _production_bundle(repo)
    return StrategyProviderBundleV1(
        QualificationRunner(
            repo,
            QUALIFICATION_FIXTURE,
            _probes(),
            live_adapter=_UnavailableQualificationAdapter(),
            clock=lambda: NOW,
        ),
        bundle.roster_capture,
        bundle.alias_manifest,
        bundle.snapshot_profile,
    )


# ---------------------------------------------------------------------------
# Setup detection
# ---------------------------------------------------------------------------


def test_is_setup_required_when_no_active_profile(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=None)  # type: ignore[arg-type]
    assert service.is_setup_required() is True


def test_is_setup_required_false_when_active_profile_exists(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=None)  # type: ignore[arg-type]
    assert service.is_setup_required() is False


def test_is_already_set_up_returns_true_with_timestamp(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=None)  # type: ignore[arg-type]
    already, activated_at = service.is_already_set_up()
    assert already is True
    assert activated_at is not None


def test_is_already_set_up_returns_false_when_empty(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=None)  # type: ignore[arg-type]
    already, activated_at = service.is_already_set_up()
    assert already is False
    assert activated_at is None


# ---------------------------------------------------------------------------
# Start setup
# ---------------------------------------------------------------------------


def test_start_setup_raises_when_already_set_up(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    jobs = StrategyJobService(repo)
    service = StrategyBootstrapService(repo, jobs=jobs)
    with pytest.raises(StrategyBootstrapAlreadySetUp):
        service.start_setup(BootstrapSubmissionV1(idempotency_key="already-set-up"))


def test_start_setup_enqueues_bootstrap_job(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    jobs = StrategyJobService(repo)
    service = StrategyBootstrapService(repo, jobs=jobs)
    submission = BootstrapSubmissionV1(idempotency_key="start-setup")
    job = service.start_setup(submission)
    assert job.job_type is StrategyJobType.BOOTSTRAP
    assert job.status is StrategyJobStatus.QUEUED
    assert repo.create_bootstrap_job(submission).job == job


def test_start_setup_replays_before_active_profile_no_op(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _empty_repo(path)
    jobs = StrategyJobService(repo)
    submission = BootstrapSubmissionV1(idempotency_key="completed-setup-retry")
    original = jobs.enqueue_bootstrap(submission)
    assert original.job is not None
    _seed(path)
    service = StrategyBootstrapService(repo, jobs=jobs)

    assert service.start_setup(submission) == original.job


def test_start_setup_same_key_is_atomic_across_service_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    _empty_repo(path)
    submission = BootstrapSubmissionV1(idempotency_key="service-concurrent")
    barrier = Barrier(2)

    def submit(_index: int):
        repo = _empty_repo(path)
        service = StrategyBootstrapService(repo, jobs=StrategyJobService(repo))
        barrier.wait()
        return service.start_setup(submission)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(submit, range(2)))

    assert jobs[0] == jobs[1]


def test_start_setup_rejects_divergent_reuse_through_atomic_repository_path(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=StrategyJobService(repo))
    original = service.start_setup(
        BootstrapSubmissionV1(idempotency_key="divergent-service")
    )

    with pytest.raises(StrategyJobConflict, match="idempotency key"):
        service.start_setup(
            BootstrapSubmissionV1(
                idempotency_key="divergent-service", parent_job_id=original.id
            )
        )


def test_clean_store_production_bundle_qualifies_captures_and_activates(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    bundle = _production_bundle(repo)
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    engine = build_stage_walk_engine(
        job.id,
        repo,
        StrategyJobType.BOOTSTRAP,
        bootstrap_providers=bundle,
    )
    complete = engine.run(job.id, claim.claim_token)

    assert complete.status is StrategyJobStatus.COMPLETE
    active = repo.active_snapshot_profile()
    assert active is not None
    assert repo.snapshot_profile(active.profile_hash) is not None
    assert repo.current_qualification_contract_digest() is not None
    readiness = StrategyReadinessService(repo, clock=NOW).evaluate()
    assert readiness.qualification.state is PrerequisiteState.READY
    assert readiness.roster.state is PrerequisiteState.READY
    assert readiness.active_profile.state is PrerequisiteState.READY
    with sqlite3.connect(str(tmp_path / "backtest.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_profiles").fetchone() == (1,)
        aliases = conn.execute(
            "SELECT COUNT(*) FROM security_alias_entries WHERE provider='yfinance'"
        ).fetchone()
        members = conn.execute(
            "SELECT COUNT(*) FROM reconstruction_roster_members"
        ).fetchone()
        assert aliases == members


def test_production_contract_uses_exchange_native_probe_metadata() -> None:
    probes = _production_probes()
    assert (
        probes["us_active"].expected_currency,
        probes["us_active"].expected_quote_unit,
        probes["us_active"].expected_timezone,
    ) == ("USD", "USD", "America/New_York")
    assert (
        probes["lse_active"].expected_currency,
        probes["lse_active"].expected_quote_unit,
        probes["lse_active"].expected_timezone,
    ) == ("GBP", "GBp", "Europe/London")


def test_datahub_csv_headers_are_normalized_for_strict_roster_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        text = "Symbol,Security,GICS Sector\nAAPL,Apple Inc.,Technology\n"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("requests.get", lambda *_args, **_kwargs: Response())
    assert _fetch_datahub_sp500() == [
        {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology"}
    ]


def test_bootstrap_failure_projects_actionable_safe_activity_detail() -> None:
    roster = _bootstrap_failure(
        RosterCaptureError("market identity metadata unavailable for CBOE"),
        JobFailureCode.REQUIRED_DATA_MISSING,
        stage="Roster capture",
    )
    assert roster.detail == (
        "Roster capture failed: market identity metadata unavailable for CBOE"
    )

    provider = _bootstrap_failure(
        ProviderFailure(FailureCode.PROVIDER_THROTTLED, "provider rate limited"),
        JobFailureCode.PROVIDER_CONTRACT_ERROR,
        stage="Historical qualification",
    )
    assert provider.detail == "Historical qualification failed: provider rate limited"

    database = _bootstrap_failure(
        sqlite3.IntegrityError("CHECK constraint failed: mic IN (...)"),
        JobFailureCode.INTEGRITY_ERROR,
        stage="Roster capture",
    )
    assert database.detail == (
        "Roster capture failed: roster evidence contains an unsupported market "
        "identifier"
    )

    response = requests.Response()
    response.status_code = 503
    request = _bootstrap_failure(
        requests.HTTPError("secret provider URL", response=response),
        JobFailureCode.PROVIDER_CONTRACT_ERROR,
        stage="Historical qualification",
    )
    assert request.detail == (
        "Historical qualification failed: required provider request was unavailable "
        "(HTTP 503)"
    )

    unexpected = _bootstrap_failure(
        RuntimeError("secret=/private/token"),
        JobFailureCode.INTEGRITY_ERROR,
        stage="Profile activation",
    )
    assert unexpected.detail == (
        "Profile activation failed: unexpected RuntimeError; check the server log "
        "for details"
    )
    assert "secret" not in unexpected.detail


def test_fixture_bundle_is_explicit_and_uses_pinned_evidence(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    bundle = StrategyProviderBundleV1.fixture(repo)
    service = StrategyBootstrapService(repo, jobs=None, providers=bundle)
    assert bundle.mode == "fixture"
    assert service.is_fixture is True
    assert bundle.qualification_runner.run().contract_digest


def test_production_fx_probe_matches_current_provider_calendar_contract() -> None:
    definition = _production_probes()["gbpusd"]

    assert definition.expected_timezone == "Europe/London"
    assert definition.expected_sessions == (
        date(2024, 1, 1),
        date(2024, 1, 2),
        date(2024, 1, 3),
    )


def test_roster_identity_conflict_fails_without_activating_profile(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    bundle = _production_bundle(repo)
    conflicting = _payload(
        RosterSource.TRADINGVIEW_US,
        [{"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "GBP"}],
    )

    def datahub() -> RosterSourcePayloadV1:
        return _payload(
            RosterSource.DATAHUB_SP500,
            [{"symbol": "AAPL", "name": "Apple", "sector": "Technology"}],
        )

    def us() -> RosterSourcePayloadV1:
        return conflicting

    def uk() -> RosterSourcePayloadV1:
        return _payload(
            RosterSource.TRADINGVIEW_UK,
            [{"symbol": "LSE:ULVR", "exchange": "LSE", "currency": "GBp"}],
        )

    conflicting_bundle = replace(
        bundle,
        roster_capture=ReconstructionRosterCaptureService(
            repo,
            (datahub, us, uk),
            lambda _symbol, _row: MarketIdentityEvidence(
                "XNAS", "USD", "USD", "test", "i" * 64
            ),
            clock=lambda: NOW,
        ),
    )
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    result = build_stage_walk_engine(
        job.id,
        repo,
        StrategyJobType.BOOTSTRAP,
        bootstrap_providers=conflicting_bundle,
    ).run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.PROVIDER_CONTRACT_ERROR
    assert result.failure_detail is not None
    assert result.failure_detail.startswith("Roster capture failed:")
    assert repo.active_snapshot_profile() is None


def test_profile_validation_failure_leaves_no_partial_active_profile(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    bundle = _production_bundle(repo)

    def invalid_profile(roster_digest: str):
        valid = bundle.snapshot_profile(roster_digest)
        detectors = list(valid.detectors)
        detectors[0] = ProfileDetectorV1(
            detector_id=detectors[0].detector_id,
            detector_api_version=detectors[0].detector_api_version,
            detector_version="0" * 64,
        )
        return valid.model_copy(update={"detectors": tuple(detectors)})

    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    result = build_stage_walk_engine(
        job.id,
        repo,
        StrategyJobType.BOOTSTRAP,
        bootstrap_providers=replace(bundle, snapshot_profile=invalid_profile),
    ).run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.INTEGRITY_ERROR
    assert repo.active_snapshot_profile() is None
    with sqlite3.connect(str(tmp_path / "backtest.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_profiles").fetchone() == (0,)


def test_stale_worker_cannot_bind_bootstrap_roster(tmp_path: Path) -> None:
    moment = [NOW]
    repo = BacktestRepository(
        db.make_connect(lambda: str(tmp_path / "backtest.db")),
        clock=lambda: moment[0].date(),
        instant_clock=lambda: moment[0],
    )
    repo.ensure_schema()
    stale = repo.acquire_or_renew_worker_lease("worker-a", ttl_seconds=30)
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job(lease=stale.fence)
    assert claim is not None
    staged = repo.set_strategy_job_current_stage(
        job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        stage=BootstrapStage.ROSTER_CAPTURE.value,
        lease=stale.fence,
    )
    moment[0] += timedelta(seconds=120)
    repo.acquire_or_renew_worker_lease("worker-b", ttl_seconds=30)
    service = StrategyBootstrapService(
        repo, jobs=None, providers=_production_bundle(repo)
    )
    with pytest.raises(Exception, match="stale"):
        service._capture_roster(
            job.id,
            claim.claim_token,
            expected_version=staged.status_version,
            lease=stale.fence,
        )
    assert repo.roster_digest_for_lineage(job.id) is None
    assert repo.active_snapshot_profile() is None


def test_cancellation_after_roster_is_acknowledged_before_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    bundle = _production_bundle(repo)
    original = ReconstructionRosterCaptureService.capture

    def capture_then_cancel(service, *args, **kwargs):
        captured = original(service, *args, **kwargs)
        job_claim = kwargs["job_claim"]
        assert job_claim is not None
        repo.request_strategy_job_cancellation(
            job_claim[0], expected_version=job_claim[2]
        )
        return captured

    monkeypatch.setattr(
        ReconstructionRosterCaptureService, "capture", capture_then_cancel
    )
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    result = build_stage_walk_engine(
        job.id,
        repo,
        StrategyJobType.BOOTSTRAP,
        bootstrap_providers=bundle,
    ).run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.CANCELLED
    assert repo.active_snapshot_profile() is None


def test_profile_activation_stage_is_non_cancellable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    bundle = _production_bundle(repo)
    original = StrategyBootstrapService._activate_profile

    def request_cancel_during_activation(service, job_id, claim_token, **kwargs):
        current = repo.strategy_job(job_id)
        unchanged = repo.request_strategy_job_cancellation(
            job_id, expected_version=current.status_version
        )
        assert unchanged.cancel_requested_at is None
        return original(service, job_id, claim_token, **kwargs)

    monkeypatch.setattr(
        StrategyBootstrapService,
        "_activate_profile",
        request_cancel_during_activation,
    )
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    result = build_stage_walk_engine(
        job.id,
        repo,
        StrategyJobType.BOOTSTRAP,
        bootstrap_providers=bundle,
    ).run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.COMPLETE
    assert repo.active_snapshot_profile() is not None


def test_terminal_activation_revalidates_latest_qualification(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(
        repo, jobs=None, providers=_production_bundle(repo)
    )
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    qualification_stage = repo.set_strategy_job_current_stage(
        job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        stage=BootstrapStage.QUALIFICATION.value,
    )
    service._run_qualification()
    roster_stage = repo.set_strategy_job_current_stage(
        job.id,
        claim.claim_token,
        expected_version=qualification_stage.status_version,
        stage=BootstrapStage.ROSTER_CAPTURE.value,
    )
    service._capture_roster(
        job.id,
        claim.claim_token,
        expected_version=roster_stage.status_version,
    )
    profile_stage = repo.set_strategy_job_current_stage(
        job.id,
        claim.claim_token,
        expected_version=roster_stage.status_version,
        stage=BootstrapStage.PROFILE_ACTIVATION.value,
    )
    latest = repo.latest_recorded_qualification()
    assert latest is not None
    repo.record_qualification(
        replace(
            latest,
            passed=False,
            failure_code=JobFailureCode.PROVIDER_UNAVAILABLE.value,
            failure_reason="Historical source unavailable",
        )
    )
    with pytest.raises(BootstrapStageFailure):
        service._activate_profile(
            job.id,
            claim.claim_token,
            expected_version=profile_stage.status_version,
        )
    assert repo.active_snapshot_profile() is None


def test_qualification_failure_retains_the_safe_recorded_reason(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(
        repo, jobs=None, providers=_unavailable_bundle(repo)
    )

    with pytest.raises(BootstrapStageFailure) as exc_info:
        service._run_qualification()

    assert exc_info.value.code is JobFailureCode.PROVIDER_UNAVAILABLE
    assert exc_info.value.detail == "Historical source unavailable"


def test_terminal_activation_requires_profile_activation_stage(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(
        repo, jobs=None, providers=_production_bundle(repo)
    )
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    qualification_stage = repo.set_strategy_job_current_stage(
        job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        stage=BootstrapStage.QUALIFICATION.value,
    )
    service._run_qualification()
    roster_stage = repo.set_strategy_job_current_stage(
        job.id,
        claim.claim_token,
        expected_version=qualification_stage.status_version,
        stage=BootstrapStage.ROSTER_CAPTURE.value,
    )
    service._capture_roster(
        job.id,
        claim.claim_token,
        expected_version=roster_stage.status_version,
    )
    with pytest.raises(BootstrapStageFailure):
        service._activate_profile(
            job.id,
            claim.claim_token,
            expected_version=roster_stage.status_version,
        )
    assert repo.active_snapshot_profile() is None


# ---------------------------------------------------------------------------
# Stage execution
# ---------------------------------------------------------------------------


def test_bootstrap_worker_completes_with_seeded_data(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    engine = build_stage_walk_engine(
        job.id,
        repo,
        StrategyJobType.BOOTSTRAP,
        bootstrap_providers=_production_bundle(repo),
    )
    result = engine.run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.COMPLETE
    assert result.current_stage is None


def test_bootstrap_worker_fails_without_qualification(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    engine = build_stage_walk_engine(
        job.id,
        repo,
        StrategyJobType.BOOTSTRAP,
        bootstrap_providers=_unavailable_bundle(repo),
    )
    result = engine.run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.PROVIDER_UNAVAILABLE


def test_bootstrap_worker_requalifies_an_unrelated_stored_contract(
    tmp_path: Path,
) -> None:
    """A manually seeded, out-of-contract qualification cannot enable capture."""
    repo = _empty_repo(tmp_path / "backtest.db")
    # Seed only qualification, no roster or identities
    with sqlite3.connect(str(tmp_path / "backtest.db")) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO historical_source_qualifications
               (contract_digest, source_versions_json, fixture_digest,
                probe_definition_digest, probe_digest, qualified_at,
                passed, failure_code, failure_reason)
               VALUES (?, ?, ?, ?, ?, ?, 1, NULL, NULL)""",
            (
                _qualification_digest(),
                current_source_versions_json(),
                FIXTURE_DIGEST,
                PROBE_DEFINITION_DIGEST,
                "3" * 64,
                NOW.isoformat(),
            ),
        )
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    engine = build_stage_walk_engine(
        job.id,
        repo,
        StrategyJobType.BOOTSTRAP,
        bootstrap_providers=_production_bundle(repo),
    )
    result = engine.run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.COMPLETE
    assert repo.active_snapshot_profile() is not None


# ---------------------------------------------------------------------------
# Stage failure
# ---------------------------------------------------------------------------


def test_bootstrap_stage_failure_carries_code_and_detail() -> None:
    failure = BootstrapStageFailure(JobFailureCode.PROVIDER_UNAVAILABLE, "test reason")
    assert failure.code is JobFailureCode.PROVIDER_UNAVAILABLE
    assert failure.detail == "test reason"


# ---------------------------------------------------------------------------
# Fixture environment
# ---------------------------------------------------------------------------


def test_is_fixture_requires_explicit_fixture_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STRATEGY_FIXTURE", "1")
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=None)  # type: ignore[arg-type]
    assert service.is_fixture is True


def test_is_fixture_returns_false_in_production(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("STRATEGY_FIXTURE", raising=False)
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=None)  # type: ignore[arg-type]
    assert service.is_fixture is False
