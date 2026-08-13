from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
import json
import sqlite3

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.backtest_repo import QualificationResult
from app.services.backtest.strategy_job import (
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobStatus,
)
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_data_qualification import (
    FIXTURE_CONTRACT_VERSION,
    REQUEST_CONTRACT_VERSION,
    current_source_versions_json,
)
from app.services.backtest.snapshot_profile import IntervalReadinessV1
from app.services.backtest.trading_calendar import TradingCalendar


NOW = datetime(2026, 8, 12, 9, 30, tzinfo=timezone.utc)
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
        db.make_connect(lambda: path),
        clock=lambda: date(2026, 8, 12),
        instant_clock=lambda: NOW,
    )
    repo.ensure_schema()
    _seed_profile(path)
    return repo


def _seed_profile(path: Path) -> None:
    """Seed the minimum valid FK graph; lifecycle tests do not read its payload."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT OR IGNORE INTO security_identity_registry_revisions
               (revision_digest, canonical_manifest_json, evidence_digest, created_at)
               VALUES (?, '{}', ?, ?)""",
            ("c" * 64, "d" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO security_alias_manifests
               (alias_revision, canonical_manifest_json, evidence_digest, created_at)
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
            """INSERT OR IGNORE INTO snapshot_profiles
               (profile_hash, canonical_profile_json, display_version, roster_digest,
                scanner_schema_version, calendar_dataset_version,
                calendar_dataset_digest, cadence)
               VALUES (?, '{}', 'Scanner data v1', ?, 'historical_scan_record.v1',
                       'exchange-calendars-v1', ?, 'per-exchange month_end')""",
            (PROFILE_HASH, ROSTER_DIGEST, TradingCalendar().session_table_digest()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO active_snapshot_profile
               (singleton_id, profile_hash, activation_seq, activated_at)
               VALUES (1, ?, 1, ?)""",
            (PROFILE_HASH, NOW.isoformat()),
        )
        digest = _qualification_digest()
        conn.execute(
            """INSERT OR IGNORE INTO historical_source_qualifications (
                   contract_digest, source_versions_json, fixture_digest,
                   probe_definition_digest, probe_digest, qualified_at, passed,
                   failure_code, failure_reason
               ) VALUES (?, ?, ?, ?, ?, ?, 1, NULL, NULL)""",
            (
                digest,
                current_source_versions_json(),
                FIXTURE_DIGEST,
                PROBE_DEFINITION_DIGEST,
                "3" * 64,
                NOW.isoformat(),
            ),
        )


def _enqueue(repo: BacktestRepository, start: str = "2026-05", end: str = "2026-07"):
    return repo.create_initialization_job(
        profile_hash=PROFILE_HASH,
        requested_start=start,
        requested_end=end,
        calendar_dataset_version="exchange-calendars-v1",
        qualification_contract_digest=_qualification_digest(),
    )


def test_create_initialization_job_is_atomic_immutable_and_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)

    result = _enqueue(repo)

    assert result.no_op is False
    assert result.job is not None
    assert result.job.status is StrategyJobStatus.QUEUED
    assert result.job.enqueue_seq == 1
    assert result.job.status_version == 1
    assert result.initialization is not None
    assert result.initialization.requested_months == (
        "2026-05",
        "2026-06",
        "2026-07",
    )
    assert result.initialization.ordered_month_digest is None

    reopened = _repo(path).strategy_job(result.job.id)
    assert reopened == result.job
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE initialization_runs SET requested_start='2026-04' WHERE job_id=?",
            (result.job.id,),
        )


def test_claim_is_fifo_single_running_and_token_owned(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    first = _enqueue(repo, "2026-05", "2026-05").job
    second = _enqueue(repo, "2026-06", "2026-06").job
    assert first is not None and second is not None

    claim = repo.claim_next_strategy_job()

    assert claim is not None
    assert claim.job.id == first.id
    assert claim.job.status is StrategyJobStatus.RUNNING
    assert claim.job.status_version == 2
    assert claim.job.claim_token == claim.claim_token
    assert repo.claim_next_strategy_job() is None
    assert repo.strategy_job(second.id).status is StrategyJobStatus.QUEUED


def test_initialization_and_backtest_placeholders_share_one_fifo(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES ('future-backtest', 'backtest', 'queued', 1, 1, ?, ?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
    initialization = _enqueue(repo, "2026-05", "2026-05").job
    assert initialization is not None and initialization.enqueue_seq == 2

    claim = repo.claim_next_strategy_job()

    assert claim is not None
    assert claim.job.id == "future-backtest"
    assert claim.initialization is None


def test_concurrent_claimers_produce_one_running_claim(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue(repo, "2026-05", "2026-05")
    _enqueue(repo, "2026-06", "2026-06")

    def claim():
        return _repo(path).claim_next_strategy_job()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))

    assert sum(item is not None for item in claims) == 1


def test_progress_and_terminal_writes_require_current_token_and_version(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    queued = _enqueue(repo).job
    assert queued is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    progress = repo.set_strategy_job_current_month(
        claim.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        month="2026-05",
    )
    assert progress.current_month == "2026-05"
    assert progress.status_version == 3

    with pytest.raises(StrategyJobConflict):
        repo.set_strategy_job_current_month(
            claim.job.id,
            "wrong-token",
            expected_version=progress.status_version,
            month="2026-06",
        )
    with pytest.raises(StrategyJobConflict):
        repo.fail_claimed_strategy_job(
            claim.job.id,
            claim.claim_token,
            expected_version=claim.job.status_version,
            failure_code=JobFailureCode.INTEGRITY_ERROR,
            failed_month="2026-05",
            detail="safe detail",
        )

    failed = repo.fail_claimed_strategy_job(
        claim.job.id,
        claim.claim_token,
        expected_version=progress.status_version,
        failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
        failed_month="2026-05",
        detail="Required historical data is unavailable",
    )
    assert failed.status is StrategyJobStatus.FAILED
    assert failed.current_month is None
    assert failed.failure_code is JobFailureCode.REQUIRED_DATA_MISSING
    with pytest.raises(StrategyJobConflict):
        repo.fail_claimed_strategy_job(
            claim.job.id,
            claim.claim_token,
            expected_version=failed.status_version,
            failure_code=JobFailureCode.INTEGRITY_ERROR,
            failed_month="2026-05",
            detail="late worker",
        )


def test_queued_and_running_cancellation_have_distinct_semantics(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    queued = _enqueue(repo, "2026-05", "2026-05").job
    running_source = _enqueue(repo, "2026-06", "2026-06").job
    assert queued is not None and running_source is not None

    cancelled = repo.request_strategy_job_cancellation(
        queued.id, expected_version=queued.status_version
    )
    assert cancelled.status is StrategyJobStatus.CANCELLED
    assert cancelled.cancel_requested_at is not None

    claim = repo.claim_next_strategy_job()
    assert claim is not None and claim.job.id == running_source.id
    requested = repo.request_strategy_job_cancellation(
        claim.job.id, expected_version=claim.job.status_version
    )
    assert requested.status is StrategyJobStatus.RUNNING
    assert requested.cancel_requested_at is not None
    terminal = repo.cancel_claimed_strategy_job(
        requested.id,
        claim.claim_token,
        expected_version=requested.status_version,
    )
    assert terminal.status is StrategyJobStatus.CANCELLED


def test_startup_reconciliation_fails_only_running_jobs(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    first = _enqueue(repo, "2026-05", "2026-05").job
    second = _enqueue(repo, "2026-06", "2026-06").job
    assert first is not None and second is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    reconciled = repo.reconcile_interrupted_strategy_jobs()

    assert [job.id for job in reconciled] == [first.id]
    failed = repo.strategy_job(first.id)
    assert failed.status is StrategyJobStatus.FAILED
    assert failed.failure_code is JobFailureCode.WORKER_INTERRUPTED
    assert repo.strategy_job(second.id).status is StrategyJobStatus.QUEUED


def test_completion_writes_final_digest_once_and_late_cancel_is_a_no_op(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    queued = _enqueue(repo, "2026-05", "2026-05").job
    assert queued is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    digest = "9" * 64
    setattr(
        repo,
        "_interval_readiness_on_connection",
        lambda *_args: IntervalReadinessV1(
            profile_hash=PROFILE_HASH,
            start_month="2026-05",
            end_month="2026-05",
            ready=True,
            no_op=True,
            missing_months=(),
            ordered_month_digest=digest,
        ),
    )

    complete = repo.complete_claimed_initialization_job(
        claim.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
    )

    assert complete.status is StrategyJobStatus.COMPLETE
    assert repo.initialization_run(complete.id).ordered_month_digest == digest
    assert (
        repo.request_strategy_job_cancellation(
            complete.id, expected_version=complete.status_version
        )
        == complete
    )
    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_initialization_job(
            complete.id,
            claim.claim_token,
            expected_version=complete.status_version,
        )


def test_cancel_intent_committed_before_completion_prevents_completion(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    queued = _enqueue(repo, "2026-05", "2026-05").job
    assert queued is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    requested = repo.request_strategy_job_cancellation(
        claim.job.id, expected_version=claim.job.status_version
    )
    setattr(
        repo,
        "_interval_readiness_on_connection",
        lambda *_args: IntervalReadinessV1(
            profile_hash=PROFILE_HASH,
            start_month="2026-05",
            end_month="2026-05",
            ready=True,
            no_op=True,
            missing_months=(),
            ordered_month_digest="8" * 64,
        ),
    )

    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_initialization_job(
            requested.id,
            claim.claim_token,
            expected_version=requested.status_version,
        )

    assert repo.initialization_run(requested.id).ordered_month_digest is None


def test_ready_interval_is_no_op_and_creates_no_job(tmp_path: Path) -> None:
    # The readiness/commit integration is covered in the initialization engine
    # suite; this checks the repository's no-write branch with an injected
    # authoritative readiness result.
    repo = _repo(tmp_path / "backtest.db")
    repo._interval_is_ready_for_job = lambda *_args, **_kwargs: True  # type: ignore[attr-defined,method-assign]

    result = _enqueue(repo, "2026-05", "2026-05")

    assert result.no_op is True
    assert result.job is None
    assert repo.list_strategy_jobs() == ()


def test_database_rejects_illegal_transitions_unversioned_writes_and_subtype_delete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    queued = _enqueue(repo, "2026-05", "2026-05").job
    assert queued is not None

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE strategy_jobs SET status='complete' WHERE id=?", (queued.id,)
        )

    claim = repo.claim_next_strategy_job()
    assert claim is not None
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE strategy_jobs SET current_month='2026-05' WHERE id=?",
            (queued.id,),
        )
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM initialization_runs WHERE job_id=?", (queued.id,))


def test_database_rejects_initialization_subtype_for_backtest_placeholder(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    _repo(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES ('backtest-1', 'backtest', 'queued', 1, 1, ?, ?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO initialization_runs (
                       job_id, profile_hash, requested_start, requested_end,
                       requested_months_json, requested_month_digest,
                       calendar_dataset_version, qualification_contract_digest
                   ) VALUES ('backtest-1', ?, '2026-05', '2026-05', '["2026-05"]',
                             ?, 'exchange-calendars-v1', ?)""",
                (PROFILE_HASH, "f" * 64, _qualification_digest()),
            )


def test_enqueue_rechecks_latest_qualification_inside_creation_transaction(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    repo.record_qualification(
        QualificationResult(
            contract_digest=_qualification_digest(),
            source_versions_json=current_source_versions_json(),
            fixture_digest=FIXTURE_DIGEST,
            probe_definition_digest=PROBE_DEFINITION_DIGEST,
            probe_digest="4" * 64,
            qualified_at=NOW.isoformat(),
            passed=False,
            failure_code="integrity_error",
            failure_reason="Historical evidence integrity check failed",
        )
    )

    with pytest.raises(StrategyJobConflict, match="not qualified"):
        _enqueue(repo, "2026-05", "2026-05")

    assert repo.list_strategy_jobs() == ()


def test_database_rejects_standalone_version_and_premature_digest_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    queued = _enqueue(repo, "2026-05", "2026-05").job
    assert queued is not None

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE strategy_jobs SET status_version=status_version+1 WHERE id=?",
            (queued.id,),
        )
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE initialization_runs SET ordered_month_digest=? WHERE job_id=?",
            ("9" * 64, queued.id),
        )
