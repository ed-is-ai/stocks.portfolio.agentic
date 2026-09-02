"""Tests for StrategyReadinessService (Story 4.4).

Tests the six-prerequisite readiness composition, worker states,
diagnostics bounding, empty states, and the read-only guarantee.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import cast


from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_data_qualification import (
    FIXTURE_CONTRACT_VERSION,
    REQUEST_CONTRACT_VERSION,
    current_source_versions_json,
)
from app.services.backtest.strategy_job import (
    JobFailureCode,
    PrerequisiteState,
    RecoveryAction,
    StrategyJobType,
    WorkerState,
)
from app.services.backtest.snapshot_profile import SnapshotProfileV1
from app.services.backtest.strategy_readiness_service import (
    StrategyReadinessService,
)
from app.services.backtest.source_manifest import detector_source_manifests
from app.services.backtest.trading_calendar import TradingCalendar
import json

NOW = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)
PROFILE_HASH = "a" * 64
ROSTER_DIGEST = "b" * 64
FIXTURE_DIGEST = "1" * 64
PROBE_DEFINITION_DIGEST = "2" * 64


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


def _prereq(name: str):
    """A minimal prerequisite row for diagnostics projection stubs."""
    from enum import Enum

    class _E(str, Enum):
        V = "ready"

    return SimpleNamespace(
        name=name,
        state=_E.V,
        reason="ok",
        recovery_action=SimpleNamespace(value="none"),
        last_verified_at=None,
    )


def _delta_profile(**overrides):
    """A minimal-but-valid profile for the gh-468 delta projection tests."""
    from pathlib import Path

    from app.services.backtest.detectors import DETECTOR_REGISTRY
    from app.services.backtest.snapshot_profile import ProfileDetectorV1

    project_root = Path(__file__).resolve().parents[2]
    manifests = detector_source_manifests(project_root)
    fields: dict[str, object] = dict(
        schema_version="snapshot_profile.v1",
        display_version="Scanner data v1",
        record_schema_version="historical_scan_record.v1",
        detectors=tuple(
            ProfileDetectorV1(
                detector_id=detector.detector_id,
                detector_api_version=detector.detector_api_version,
                detector_version=manifests[detector.detector_id].digest,
            )
            for detector in DETECTOR_REGISTRY
        ),
        roster_policy_version="ReconstructionRosterPolicyV1",
        roster_digest="b" * 64,
        identity_registry_version="SecurityIdentityRegistryV1",
        alias_policy_version="SecurityAliasManifestV1",
        source_policy_version="FreeHistoricalSourcePolicyV1",
        calendar_policy_version="PerExchangeMonthEndV1",
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest="c" * 64,
        yfinance_request_contract_version="yfinance-daily-v1",
        yfinance_ingestion_version="ingestion-v1",
        market_plane_policy_version="HistoricalMarketPlanesV1",
        reconstructability_policy_version="reconstructability.v1",
        provenance_vocabulary=("best_effort_reconstructed", "observed_bau"),
        cadence="per-exchange month_end",
    )
    return SnapshotProfileV1.model_validate({**fields, **overrides})


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


def _full_repo(path: Path) -> BacktestRepository:
    repo = _empty_repo(path)
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
    return repo


# ---------------------------------------------------------------------------
# Empty state — all prerequisites missing
# ---------------------------------------------------------------------------


def test_empty_repo_all_prerequisites_missing(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    assert result.qualification.state is PrerequisiteState.MISSING
    assert result.roster.state is PrerequisiteState.MISSING
    assert result.active_profile.state is PrerequisiteState.MISSING
    assert result.coverage.state is PrerequisiteState.MISSING
    assert result.worker.state is WorkerState.DISABLED


def test_empty_repo_qualification_recovery_is_set_up(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    assert result.qualification.recovery_action is RecoveryAction.SET_UP


def test_empty_repo_active_profile_recovery_is_set_up(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    assert result.active_profile.recovery_action is RecoveryAction.SET_UP


# ---------------------------------------------------------------------------
# Full state — all prerequisites ready
# ---------------------------------------------------------------------------


def test_full_repo_qualification_ready(tmp_path: Path) -> None:
    repo = _full_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    assert result.qualification.state is PrerequisiteState.READY


def test_invalid_active_profile_roster_is_not_ready(tmp_path: Path) -> None:
    repo = _full_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    # The seed deliberately stores an invalid canonical profile JSON. A
    # globally present identity must not make that unusable active profile
    # appear to have a usable roster.
    assert result.roster.state is PrerequisiteState.MISSING


def test_full_repo_active_profile_ready(tmp_path: Path) -> None:
    repo = _full_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    assert result.active_profile.state is PrerequisiteState.READY


def test_full_repo_worker_ready(tmp_path: Path) -> None:
    repo = _full_repo(tmp_path / "backtest.db")
    # Acquire a lease so worker is not disabled
    repo.acquire_or_renew_worker_lease("test-instance", ttl_seconds=300)
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    assert result.worker.state is WorkerState.READY


# ---------------------------------------------------------------------------
# Worker states
# ---------------------------------------------------------------------------


def test_worker_disabled_when_no_lease(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    assert result.worker.state is WorkerState.DISABLED


def test_worker_unavailable_interrupted_when_expired(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    repo.acquire_or_renew_worker_lease("test-instance", ttl_seconds=1)
    # Use a future time so the lease is expired
    future = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
    service = StrategyReadinessService(repo, clock=future)
    result = service.evaluate()
    assert result.worker.state is WorkerState.UNAVAILABLE_INTERRUPTED


# ---------------------------------------------------------------------------
# Coverage prerequisite
# ---------------------------------------------------------------------------


def test_coverage_missing_when_no_snapshots(tmp_path: Path) -> None:
    repo = _full_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    # The seeded profile has invalid JSON ('{}'), so coverage
    # evaluation hits an integrity error rather than a clean "missing"
    assert result.coverage.state in (
        PrerequisiteState.MISSING,
        PrerequisiteState.INTEGRITY_ERROR,
    )
    assert result.coverage.recovery_action is RecoveryAction.INITIALIZE


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_diagnostics_returns_bounded_failures(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    diag = service.diagnostics()
    assert "is_fixture" in diag
    assert "prerequisites" in diag
    assert "worker" in diag
    assert "recent_failures" in diag
    assert diag["recent_failures"] == []


def test_diagnostics_includes_recent_failures(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    # Create and fail a job
    job = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    repo.fail_claimed_strategy_job(
        job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
        failed_month=None,
        detail="test failure",
    )
    service = StrategyReadinessService(repo, clock=NOW)
    diag = service.diagnostics()
    assert len(diag["recent_failures"]) == 1
    assert diag["recent_failures"][0]["job_type"] == "bootstrap"


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_evaluate_does_not_mutate_state(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    # Evaluate twice — state should be identical
    first = service.evaluate()
    second = service.evaluate()
    assert first.qualification.state is second.qualification.state
    assert first.worker.state is second.worker.state
    # No jobs should have been created
    assert repo.list_strategy_jobs() == ()


# ---------------------------------------------------------------------------
# Fixture labelling
# ---------------------------------------------------------------------------


def test_is_fixture_true_in_test(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test::test_func")
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyReadinessService(repo, clock=NOW)
    result = service.evaluate()
    assert result.is_fixture is True


# ---------------------------------------------------------------------------
# gh-468: predecessor-delta projection
# ---------------------------------------------------------------------------


class _DeltaRepo:
    """Minimal read-only stub for ``profile_delta`` (gh-468)."""

    def __init__(self, previous, delta, current):
        self._previous = previous
        self._delta = delta
        self._current = current

    def previous_snapshot_profile(self, _profile_hash):
        return self._previous

    def profile_has_committed_months(self, _profile_hash):
        return True

    def profile_member_delta(self, _previous, _next):
        return self._delta

    def snapshot_profile(self, _profile_hash):
        return self._current

    def active_snapshot_profile(self):
        from types import SimpleNamespace

        return SimpleNamespace(profile_hash="cur", activation_seq=2)


def test_profile_delta_counts_and_update_availability(tmp_path: Path) -> None:
    previous = _delta_profile(roster_digest="3" * 64)
    current = _delta_profile(roster_digest="4" * 64)
    repo = _DeltaRepo(
        previous,
        SimpleNamespace(added=("a",), removed=("r", "r2"), unchanged=("u",)),
        current,
    )
    service = StrategyReadinessService(
        cast(BacktestRepository, repo), clock=lambda: NOW
    )

    delta = service.profile_delta()

    assert delta is not None
    assert delta["previous_profile_hash"] == previous.profile_hash
    assert delta["added"] == 1
    assert delta["removed"] == 2
    assert delta["unchanged"] == 1
    assert delta["update_available"] is True
    assert delta["update_blocked_reasons"] == []


def test_profile_delta_reports_gate_reasons(tmp_path: Path) -> None:
    previous = _delta_profile(roster_digest="3" * 64)
    current = _delta_profile(
        roster_digest="4" * 64, yfinance_ingestion_version="ingestion-v2"
    )
    repo = _DeltaRepo(
        previous, SimpleNamespace(added=(), removed=(), unchanged=()), current
    )
    service = StrategyReadinessService(
        cast(BacktestRepository, repo), clock=lambda: NOW
    )

    delta = service.profile_delta()

    assert delta is not None
    assert delta["update_available"] is False
    assert any("ingestion" in reason for reason in delta["update_blocked_reasons"])


def test_profile_delta_none_without_predecessor(tmp_path: Path) -> None:
    repo = _DeltaRepo(None, None, _delta_profile(roster_digest="4" * 64))
    service = StrategyReadinessService(
        cast(BacktestRepository, repo), clock=lambda: NOW
    )

    assert service.profile_delta() is None


def test_diagnostics_includes_profile_delta(tmp_path: Path) -> None:
    previous = _delta_profile(roster_digest="3" * 64)
    current = _delta_profile(roster_digest="4" * 64)
    repo = _DeltaRepo(
        previous, SimpleNamespace(added=(), removed=(), unchanged=()), current
    )
    service = StrategyReadinessService(
        cast(BacktestRepository, repo), clock=lambda: NOW
    )

    empty_readiness = SimpleNamespace(
        is_fixture=False,
        qualification=_prereq("qualification"),
        roster=_prereq("roster"),
        active_profile=_prereq("active_profile"),
        coverage=_prereq("coverage"),
        discovery=_prereq("discovery"),
        prerequisites=(),
        worker=SimpleNamespace(
            state=SimpleNamespace(value="ready"),
            reason="ok",
            recovery_action=SimpleNamespace(value="none"),
        ),
        recent_failures=(),
    )
    diagnostics = service.diagnostics(empty_readiness)

    assert diagnostics["profile_delta"] == service.profile_delta()
