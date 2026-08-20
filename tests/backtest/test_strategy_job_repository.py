from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import sqlite3

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.backtest_repo import QualificationResult
from app.services.backtest.strategy_job import (
    BacktestEnqueueResultV1,
    BacktestSubmissionV1,
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobNotFound,
    StrategyJobStatus,
    StrategyJobType,
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


#: Must equal ``manifest_digest(json.loads(canonical_manifest_json))`` for
#: the fixed ``"{}"`` payload every ``_enqueue_backtest`` submission below
#: uses -- ``create_backtest_job`` now verifies that equality itself
#: (Story 2.6 review) rather than trusting a caller-supplied digest.
BACKTEST_MANIFEST_DIGEST = manifest_digest(json.loads("{}"))
BACKTEST_EXECUTION_CONTRACT_DIGEST = "8" * 64
BACKTEST_STRATEGY_SOURCE_DIGEST = "7" * 64
BACKTEST_ORDERED_MONTH_DIGEST = "6" * 64


def _seed_run_input_manifest(
    path: Path,
    digest: str = BACKTEST_MANIFEST_DIGEST,
    execution_contract_digest: str = BACKTEST_EXECUTION_CONTRACT_DIGEST,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO run_input_manifests
               (digest, execution_contract_digest, canonical_manifest_json, created_at)
               VALUES (?, ?, '{}', ?)""",
            (digest, execution_contract_digest, NOW.isoformat()),
        )


def _seed_strategy_run(
    path: Path,
    job_id: str,
    *,
    start_month: str = "2026-05",
    end_month: str = "2026-05",
) -> None:
    _seed_run_input_manifest(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO strategy_runs (
                   id, strategy_id, strategy_api_version, strategy_source_digest,
                   parameters_json, profile_hash, start_month, end_month,
                   ordered_month_digest, base_currency, starting_capital,
                   run_input_manifest_digest, execution_contract_digest, created_at
               ) VALUES (?, 'momentum_v1', 1, ?, '{"lookback": 20}', ?, ?, ?, ?,
                         'USD', '10000.00000000', ?, ?, ?)""",
            (
                job_id,
                BACKTEST_STRATEGY_SOURCE_DIGEST,
                PROFILE_HASH,
                start_month,
                end_month,
                BACKTEST_ORDERED_MONTH_DIGEST,
                BACKTEST_MANIFEST_DIGEST,
                BACKTEST_EXECUTION_CONTRACT_DIGEST,
                NOW.isoformat(),
            ),
        )


def _patch_ready(
    repo: BacktestRepository, digest: str = BACKTEST_ORDERED_MONTH_DIGEST
) -> None:
    """Inject an authoritative Ready coverage result, mirroring
    ``test_completion_writes_final_digest_once_and_late_cancel_is_a_no_op``'s
    established pattern -- assembling real committed snapshot months is
    the initialization engine suite's concern, not this lifecycle suite's."""

    def _readiness(
        _conn: object, profile_hash: str, start_month: str, end_month: str
    ) -> IntervalReadinessV1:
        return IntervalReadinessV1(
            profile_hash=profile_hash,
            start_month=start_month,
            end_month=end_month,
            ready=True,
            no_op=True,
            missing_months=(),
            ordered_month_digest=digest,
        )

    repo._interval_readiness_on_connection = _readiness  # type: ignore[method-assign]


def _enqueue_backtest(
    repo: BacktestRepository,
    *,
    start_month: str = "2026-05",
    end_month: str = "2026-05",
    idempotency_key: str | None = None,
    starting_capital: str = "10000",
    parent_job_id: str | None = None,
) -> BacktestEnqueueResultV1:
    _patch_ready(repo)
    return repo.create_backtest_job(
        BacktestSubmissionV1(
            strategy_id="momentum_v1",
            strategy_api_version=1,
            strategy_source_digest=BACKTEST_STRATEGY_SOURCE_DIGEST,
            parameters={"lookback": 20},
            profile_hash=PROFILE_HASH,
            start_month=start_month,
            end_month=end_month,
            base_currency="USD",
            starting_capital=Decimal(starting_capital),
            run_input_manifest_digest=BACKTEST_MANIFEST_DIGEST,
            execution_contract_digest=BACKTEST_EXECUTION_CONTRACT_DIGEST,
            canonical_manifest_json="{}",
            idempotency_key=idempotency_key,
            parent_job_id=parent_job_id,
        )
    )


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
    _seed_strategy_run(path, "future-backtest")
    initialization = _enqueue(repo, "2026-05", "2026-05").job
    assert initialization is not None and initialization.enqueue_seq == 2

    claim = repo.claim_next_strategy_job()

    assert claim is not None
    assert claim.job.id == "future-backtest"
    assert claim.initialization is None
    assert claim.backtest is not None
    assert claim.backtest.job_id == "future-backtest"


def test_claim_of_subtype_less_backtest_placeholder_raises_typed_error(
    tmp_path: Path,
) -> None:
    """The schema deliberately keeps no trigger forbidding a subtype-less
    ``job_type='backtest'`` row (Story 2.2/2.3's original lightweight FIFO
    placeholder, still legal at the SQL level) -- but once Story 2.6 wires
    real claim/subtype loading, ``ClaimedStrategyJobV1`` requires a
    matching ``BacktestRunV1`` exactly as it already does for
    initialization, so claiming one now surfaces a clear typed error
    (and rolls back the claim) instead of silently claiming with no data.
    """
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES ('orphan-backtest', 'backtest', 'queued', 1, 1, ?, ?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )

    with pytest.raises(StrategyJobNotFound):
        repo.claim_next_strategy_job()

    assert repo.strategy_job("orphan-backtest").status is StrategyJobStatus.QUEUED


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


# ---------------------------------------------------------------------------
# Story 2.6: Backtest atomic enqueue, type-aware claim/progress/fail/cancel,
# restart, and tombstone delete.
# ---------------------------------------------------------------------------


def test_create_backtest_job_is_atomic_and_persists_run_and_manifest_binding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)

    result = _enqueue_backtest(repo)

    assert result.job.status is StrategyJobStatus.QUEUED
    assert result.job.job_type is StrategyJobType.BACKTEST
    assert result.job.enqueue_seq == 1
    assert result.backtest.job_id == result.job.id
    assert result.backtest.strategy_id == "momentum_v1"
    assert result.backtest.parameters == {"lookback": 20}
    assert result.backtest.ordered_month_digest == BACKTEST_ORDERED_MONTH_DIGEST
    assert result.backtest.starting_capital == Decimal("10000")

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM strategy_runs WHERE id=?", (result.job.id,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM run_input_manifests WHERE digest=?",
            (BACKTEST_MANIFEST_DIGEST,),
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE strategy_runs SET starting_capital='1' WHERE id=?",
                (result.job.id,),
            )

    reopened = _repo(path).strategy_job(result.job.id)
    assert reopened == result.job


def test_create_backtest_job_idempotency_key_returns_the_same_attempt(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")

    first = _enqueue_backtest(repo, idempotency_key="submit-1")
    second = _enqueue_backtest(repo, idempotency_key="submit-1")

    assert second.job == first.job
    assert len(repo.list_strategy_jobs()) == 1


def test_create_backtest_job_idempotency_key_rejects_a_divergent_resubmission(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")

    _enqueue_backtest(repo, idempotency_key="submit-1", starting_capital="10000")

    with pytest.raises(StrategyJobConflict, match="idempotency key"):
        _enqueue_backtest(repo, idempotency_key="submit-1", starting_capital="20000")
    assert len(repo.list_strategy_jobs()) == 1


def test_create_backtest_job_without_a_key_always_creates_distinct_attempts(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")

    first = _enqueue_backtest(repo)
    second = _enqueue_backtest(repo)

    assert first.job.id != second.job.id
    assert len(repo.list_strategy_jobs()) == 2


def test_create_backtest_job_reuses_an_existing_manifest_digest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)

    _enqueue_backtest(repo)
    _enqueue_backtest(repo)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM run_input_manifests WHERE digest=?",
            (BACKTEST_MANIFEST_DIGEST,),
        ).fetchone() == (1,)


def test_create_backtest_job_rejects_a_manifest_digest_mismatch(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    _patch_ready(repo)

    with pytest.raises(StrategyJobConflict, match="run input manifest digest"):
        repo.create_backtest_job(
            BacktestSubmissionV1(
                strategy_id="momentum_v1",
                strategy_api_version=1,
                strategy_source_digest=BACKTEST_STRATEGY_SOURCE_DIGEST,
                parameters={"lookback": 20},
                profile_hash=PROFILE_HASH,
                start_month="2026-05",
                end_month="2026-05",
                base_currency="USD",
                starting_capital=Decimal("10000"),
                run_input_manifest_digest="1" * 64,
                execution_contract_digest=BACKTEST_EXECUTION_CONTRACT_DIGEST,
                canonical_manifest_json="{}",
            )
        )
    assert repo.list_strategy_jobs() == ()


def test_create_backtest_job_rejects_an_inactive_profile(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    other_profile = "z" * 64
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO snapshot_profiles (
                   profile_hash, canonical_profile_json, display_version, roster_digest,
                   scanner_schema_version, calendar_dataset_version,
                   calendar_dataset_digest, cadence
               ) VALUES (?, '{}', 'Scanner data v2', ?, 'historical_scan_record.v1',
                         'exchange-calendars-v1', ?, 'per-exchange month_end')""",
            (other_profile, ROSTER_DIGEST, TradingCalendar().session_table_digest()),
        )
        conn.execute(
            """UPDATE active_snapshot_profile
               SET profile_hash=?, activation_seq=activation_seq+1
               WHERE singleton_id=1""",
            (other_profile,),
        )

    with pytest.raises(StrategyJobConflict, match="not active"):
        _enqueue_backtest(repo)


def test_create_backtest_job_rejects_when_coverage_is_not_ready(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")

    with pytest.raises(StrategyJobConflict, match="not Ready"):
        repo.create_backtest_job(
            BacktestSubmissionV1(
                strategy_id="momentum_v1",
                strategy_api_version=1,
                strategy_source_digest=BACKTEST_STRATEGY_SOURCE_DIGEST,
                parameters={"lookback": 20},
                profile_hash=PROFILE_HASH,
                start_month="2026-05",
                end_month="2026-05",
                base_currency="USD",
                starting_capital=Decimal("10000"),
                run_input_manifest_digest=BACKTEST_MANIFEST_DIGEST,
                execution_contract_digest=BACKTEST_EXECUTION_CONTRACT_DIGEST,
                canonical_manifest_json="{}",
            )
        )
    assert repo.list_strategy_jobs() == ()


def test_create_backtest_job_rejects_a_non_terminal_parent(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    parent = _enqueue_backtest(repo).job

    with pytest.raises(StrategyJobConflict, match="must be terminal"):
        _enqueue_backtest(repo, parent_job_id=parent.id)


def test_create_backtest_job_rejects_a_non_backtest_parent(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    initialization = _enqueue(repo, "2026-05", "2026-05").job
    assert initialization is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    failed = repo.fail_claimed_strategy_job(
        claim.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
        failed_month="2026-05",
        detail="Required historical data is unavailable",
    )

    with pytest.raises(StrategyJobConflict, match="must be a backtest job"):
        _enqueue_backtest(repo, parent_job_id=failed.id)


def test_backtest_claim_loads_matching_run_and_no_initialization_subtype(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    enqueued = _enqueue_backtest(repo)

    claim = repo.claim_next_strategy_job()

    assert claim is not None
    assert claim.job.id == enqueued.job.id
    assert claim.initialization is None
    assert claim.backtest is not None
    assert claim.backtest == enqueued.backtest


def test_mixed_fifo_claims_by_smallest_enqueue_seq_regardless_of_type(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    # Initialization is enqueued first, and while it is under readiness's
    # real (unpatched) authority; a backtest submission second, deliberately
    # patching readiness only after the initialization job already exists,
    # so the shared FIFO's ordering -- not either type's own readiness
    # semantics -- is what this test isolates.
    initialization = _enqueue(repo, "2026-06", "2026-06").job
    assert initialization is not None
    backtest = _enqueue_backtest(repo, start_month="2026-05", end_month="2026-05")
    assert backtest.job.enqueue_seq > initialization.enqueue_seq

    claim = repo.claim_next_strategy_job()

    assert claim is not None
    assert claim.job.id == initialization.id
    assert repo.strategy_job(backtest.job.id).status is StrategyJobStatus.QUEUED


def test_backtest_progress_validates_against_strategy_run_range(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    _enqueue_backtest(repo, start_month="2026-05", end_month="2026-06")
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    with pytest.raises(StrategyJobConflict, match="outside requested range"):
        repo.set_strategy_job_current_month(
            claim.job.id,
            claim.claim_token,
            expected_version=claim.job.status_version,
            month="2026-07",
        )

    progressed = repo.set_strategy_job_current_month(
        claim.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        month="2026-05",
    )
    assert progressed.current_month == "2026-05"


def test_backtest_failed_month_validates_against_strategy_run_range(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    _enqueue_backtest(repo, start_month="2026-05", end_month="2026-05")
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    with pytest.raises(StrategyJobConflict, match="outside requested range"):
        repo.fail_claimed_strategy_job(
            claim.job.id,
            claim.claim_token,
            expected_version=claim.job.status_version,
            failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
            failed_month="2026-06",
            detail="safe detail",
        )

    failed = repo.fail_claimed_strategy_job(
        claim.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
        failed_month="2026-05",
        detail="Required historical data is unavailable",
    )
    assert failed.failed_month == "2026-05"


def test_backtest_running_cancellation_deletes_staging_atomically(
    tmp_path: Path,
) -> None:
    from app.services.backtest.backtest_engine import EquityCurvePointV1

    path = tmp_path / "backtest.db"
    repo = _repo(path)
    enqueued = _enqueue_backtest(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    repo.write_backtest_staging(
        claim.job.id,
        claim_token=claim.claim_token,
        expected_version=claim.job.status_version,
        state_schema_version="backtest_portfolio_state.v1",
        portfolio_state={"cash": "10000.00000000", "positions": []},
        events=(),
        equity_curve=(
            EquityCurvePointV1(
                session=date(2026, 5, 4),
                cash_base=Decimal("10000"),
                positions_value_base=Decimal("0"),
                total_equity_base=Decimal("10000"),
                sequence=1,
            ),
        ),
        final_cash_base=Decimal("10000"),
    )
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (enqueued.job.id,)
            ).fetchone()
            is not None
        )

    requested = repo.request_strategy_job_cancellation(
        claim.job.id, expected_version=claim.job.status_version
    )
    assert requested.status is StrategyJobStatus.RUNNING
    cancelled = repo.cancel_claimed_strategy_job(
        claim.job.id, claim.claim_token, expected_version=requested.status_version
    )

    assert cancelled.status is StrategyJobStatus.CANCELLED
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (enqueued.job.id,)
            ).fetchone()
            is None
        )
        # Shared, content-addressed evidence outlives the cancelled attempt.
        assert conn.execute(
            "SELECT COUNT(*) FROM run_input_manifests WHERE digest=?",
            (BACKTEST_MANIFEST_DIGEST,),
        ).fetchone() == (1,)


def test_restart_backtest_job_is_idempotent_and_replays_from_beginning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    source = _enqueue_backtest(repo, start_month="2026-05", end_month="2026-07")
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    failed = repo.fail_claimed_strategy_job(
        claim.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
        failed_month="2026-05",
        detail="Required historical data is unavailable",
    )

    first = repo.restart_backtest_job(
        source.job.id, expected_version=failed.status_version, idempotency_key="retry-1"
    )
    assert first.job.status is StrategyJobStatus.QUEUED
    assert first.job.parent_job_id == source.job.id
    assert first.backtest.strategy_id == source.backtest.strategy_id
    assert first.backtest.parameters == source.backtest.parameters
    assert first.backtest.start_month == "2026-05"
    assert first.backtest.end_month == "2026-07"
    assert first.backtest.run_input_manifest_digest == BACKTEST_MANIFEST_DIGEST
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (first.job.id,)
            ).fetchone()
            is None
        )
        # The manifest digest is reused, never duplicated.
        assert conn.execute(
            "SELECT COUNT(*) FROM run_input_manifests WHERE digest=?",
            (BACKTEST_MANIFEST_DIGEST,),
        ).fetchone() == (1,)

    repeated = repo.restart_backtest_job(
        source.job.id, expected_version=failed.status_version, idempotency_key="retry-1"
    )
    assert repeated.job == first.job
    assert len(repo.list_strategy_jobs()) == 2

    with pytest.raises(StrategyJobConflict, match="already has"):
        repo.restart_backtest_job(
            source.job.id,
            expected_version=failed.status_version,
            idempotency_key="retry-2",
        )


def test_restart_backtest_job_rejects_a_non_backtest_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    initialization = _enqueue(repo, "2026-05", "2026-05").job
    assert initialization is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    failed = repo.fail_claimed_strategy_job(
        claim.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
        failed_month="2026-05",
        detail="Required historical data is unavailable",
    )

    with pytest.raises(StrategyJobConflict, match="requires a backtest job"):
        repo.restart_backtest_job(
            failed.id, expected_version=failed.status_version, idempotency_key="retry-1"
        )


def test_delete_backtest_job_tombstones_run_and_keeps_shared_manifest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    source = _enqueue_backtest(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    failed = repo.fail_claimed_strategy_job(
        claim.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        failure_code=JobFailureCode.REQUIRED_DATA_MISSING,
        failed_month="2026-05",
        detail="Required historical data is unavailable",
    )

    deleted = repo.delete_strategy_job(
        source.job.id, expected_version=failed.status_version
    )

    assert deleted.deleted_at is not None
    assert deleted.audit_summary is not None
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM strategy_runs WHERE id=?", (source.job.id,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM run_input_manifests WHERE digest=?",
            (BACKTEST_MANIFEST_DIGEST,),
        ).fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM snapshot_profiles").fetchone() == (1,)


def test_delete_backtest_job_rejects_running_and_completed_jobs(
    tmp_path: Path,
) -> None:
    from app.services.backtest.backtest_engine import EquityCurvePointV1

    repo = _repo(tmp_path / "backtest.db")
    _enqueue_backtest(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    with pytest.raises(StrategyJobConflict, match="cannot be deleted"):
        repo.delete_strategy_job(
            claim.job.id, expected_version=claim.job.status_version
        )

    repo.write_backtest_staging(
        claim.job.id,
        claim_token=claim.claim_token,
        expected_version=claim.job.status_version,
        state_schema_version="backtest_portfolio_state.v1",
        portfolio_state={"cash": "10000.00000000", "positions": []},
        events=(),
        equity_curve=(
            EquityCurvePointV1(
                session=date(2026, 5, 4),
                cash_base=Decimal("10000"),
                positions_value_base=Decimal("0"),
                total_equity_base=Decimal("10000"),
                sequence=1,
            ),
        ),
        final_cash_base=Decimal("10000"),
    )
    completed = repo.complete_claimed_backtest_job(
        claim.job.id, claim.claim_token, expected_version=claim.job.status_version
    )
    assert completed.status is StrategyJobStatus.COMPLETE

    with pytest.raises(StrategyJobConflict, match="cannot be deleted"):
        repo.delete_strategy_job(
            completed.id, expected_version=completed.status_version
        )


# ---------------------------------------------------------------------------
# Story 2.8: ``list_backtest_activities()`` -- the Backtest activity/list
# projection.
# ---------------------------------------------------------------------------


def _complete_backtest(repo: BacktestRepository, job_id: str) -> None:
    """Claim, stage, and complete one already-queued Backtest attempt --
    exact same sequence as ``test_delete_backtest_job_rejects_running_and_
    completed_jobs`` above, factored out for reuse."""
    from app.services.backtest.backtest_engine import EquityCurvePointV1

    claim = repo.claim_next_strategy_job()
    assert claim is not None and claim.job.id == job_id
    repo.write_backtest_staging(
        claim.job.id,
        claim_token=claim.claim_token,
        expected_version=claim.job.status_version,
        state_schema_version="backtest_portfolio_state.v1",
        portfolio_state={"cash": "10000.00000000", "positions": []},
        events=(),
        equity_curve=(
            EquityCurvePointV1(
                session=date(2026, 5, 4),
                cash_base=Decimal("10000"),
                positions_value_base=Decimal("0"),
                total_equity_base=Decimal("10000"),
                sequence=1,
            ),
        ),
        final_cash_base=Decimal("10000"),
    )
    repo.complete_claimed_backtest_job(
        claim.job.id, claim.claim_token, expected_version=claim.job.status_version
    )


def _seed_bare_job(
    path: Path,
    job_id: str,
    *,
    status: str,
    enqueue_seq: int,
    status_version: int = 1,
) -> None:
    """Insert a minimal ``strategy_jobs`` row directly -- used only to
    construct the two integrity-error scenarios ``list_backtest_activities``
    must reject (a status/Result-cardinality mismatch that
    ``complete_claimed_backtest_job`` itself could never produce)."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES (?, 'backtest', ?, ?, ?, ?, ?)""",
            (
                job_id,
                status,
                enqueue_seq,
                status_version,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


def _seed_bare_backtest_result(path: Path, job_id: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO backtest_results (
                   run_id, metrics_json, final_cash_base, result_digest,
                   note, note_version, completed_at, updated_at
               ) VALUES (?, '{}', '10000.00000000', ?, NULL, 1, ?, ?)""",
            (job_id, "9" * 64, NOW.isoformat(), NOW.isoformat()),
        )


def test_list_backtest_activities_empty(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    assert repo.list_backtest_activities() == ()


def test_list_backtest_activities_excludes_initialization_jobs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue(repo, "2026-05", "2026-05")
    backtest = _enqueue_backtest(repo)

    activities = repo.list_backtest_activities()

    assert [item.job.id for item in activities] == [backtest.job.id]


def test_list_backtest_activities_strict_reverse_enqueue_seq_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    first = _enqueue_backtest(repo, start_month="2026-05", end_month="2026-05")
    second = _enqueue_backtest(repo, start_month="2026-06", end_month="2026-06")

    activities = repo.list_backtest_activities()

    assert [item.job.id for item in activities] == [second.job.id, first.job.id]
    assert activities[0].job.enqueue_seq > activities[1].job.enqueue_seq


def test_list_backtest_activities_parameter_summary_from_persisted_parameters(
    tmp_path: Path,
) -> None:
    """The summary is built from each job's own persisted
    ``strategy_runs.parameters_json`` (``_enqueue_backtest`` always writes
    ``{"lookback": 20}``), independent of whether the current Skill still
    discovers ``momentum_v1`` at all -- discovery is never consulted here."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue_backtest(repo)

    activities = repo.list_backtest_activities()

    assert activities[0].parameter_summary == "lookback=20"
    assert activities[0].strategy_id == "momentum_v1"
    assert activities[0].strategy_api_version == 1


def test_list_backtest_activities_metrics_present_only_for_complete_job(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    # The FIFO claims smallest enqueue_seq first, so the *first* enqueued
    # attempt is the one that gets claimed/completed below.
    to_complete = _enqueue_backtest(repo, start_month="2026-05", end_month="2026-05")
    still_queued = _enqueue_backtest(repo, start_month="2026-06", end_month="2026-06")
    _complete_backtest(repo, to_complete.job.id)

    activities = {item.job.id: item for item in repo.list_backtest_activities()}

    assert activities[still_queued.job.id].metrics is None
    assert activities[still_queued.job.id].metric_availability is None
    completed = activities[to_complete.job.id]
    assert completed.job.status is StrategyJobStatus.COMPLETE
    assert completed.metrics is not None
    assert completed.metric_availability is not None


def test_list_backtest_activities_rejects_complete_job_missing_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_bare_job(path, "complete-without-result", status="complete", enqueue_seq=1)
    _seed_strategy_run(path, "complete-without-result")

    with pytest.raises(BacktestIntegrityError, match="Result cardinality"):
        repo.list_backtest_activities()


def test_list_backtest_activities_rejects_noncomplete_job_with_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_bare_job(path, "queued-with-result", status="queued", enqueue_seq=1)
    _seed_strategy_run(path, "queued-with-result")
    _seed_bare_backtest_result(path, "queued-with-result")

    with pytest.raises(BacktestIntegrityError, match="Result cardinality"):
        repo.list_backtest_activities()
