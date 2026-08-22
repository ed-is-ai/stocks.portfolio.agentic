"""Tests for StrategyBootstrapService (Story 4.3).

Tests the Bootstrap setup lifecycle: setup detection, idempotent no-op,
stage execution, failure handling, and fixture environment labelling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_data_qualification import (
    FIXTURE_CONTRACT_VERSION,
    REQUEST_CONTRACT_VERSION,
    current_source_versions_json,
)
from app.services.backtest.strategy_bootstrap_service import (
    BootstrapStageFailure,
    StrategyBootstrapAlreadySetUp,
    StrategyBootstrapService,
)
from app.services.backtest.strategy_job import (
    JobFailureCode,
    StrategyJobStatus,
    StrategyJobType,
)
from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.worker import build_stage_walk_engine
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
        service.start_setup()


def test_start_setup_enqueues_bootstrap_job(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    jobs = StrategyJobService(repo)
    service = StrategyBootstrapService(repo, jobs=jobs)
    job = service.start_setup()
    assert job.job_type is StrategyJobType.BOOTSTRAP
    assert job.status is StrategyJobStatus.QUEUED


# ---------------------------------------------------------------------------
# Stage execution
# ---------------------------------------------------------------------------


def test_bootstrap_worker_completes_with_seeded_data(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    job = repo.create_bootstrap_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    engine = build_stage_walk_engine(job.id, repo, StrategyJobType.BOOTSTRAP)
    result = engine.run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.COMPLETE
    assert result.current_stage is None


def test_bootstrap_worker_fails_without_qualification(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    job = repo.create_bootstrap_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    engine = build_stage_walk_engine(job.id, repo, StrategyJobType.BOOTSTRAP)
    result = engine.run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.PROVIDER_UNAVAILABLE


def test_bootstrap_worker_fails_without_roster(tmp_path: Path) -> None:
    """Qualification exists but no roster/identities."""
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
    job = repo.create_bootstrap_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    engine = build_stage_walk_engine(job.id, repo, StrategyJobType.BOOTSTRAP)
    result = engine.run(job.id, claim.claim_token)
    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.REQUIRED_DATA_MISSING


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


def test_is_fixture_returns_true_in_test(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test::test_func")
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=None)  # type: ignore[arg-type]
    assert service.is_fixture is True


def test_is_fixture_returns_false_in_production(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("STRATEGY_FIXTURE", raising=False)
    repo = _empty_repo(tmp_path / "backtest.db")
    service = StrategyBootstrapService(repo, jobs=None)  # type: ignore[arg-type]
    assert service.is_fixture is False
