from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
from threading import Barrier

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.backtest_repo import QualificationResult
from app.services.backtest.strategy_job import (
    BacktestEnqueueResultV1,
    BacktestSubmissionV1,
    BootstrapSubmissionV1,
    BootstrapStage,
    JobFailureCode,
    PreparationStage,
    PreparationSubmissionV1,
    RunUniverseSelectionV1,
    STAGE_VALUES,
    StrategyJobConflict,
    StrategyJobNotFound,
    StrategyJobStatus,
    StrategyJobType,
    WorkerLeaseFenceV1,
)
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.run_universe import run_universe_digest
from app.services.backtest.run_input_manifest import (
    PinnedSecurityEvidenceV1,
    build_run_input_manifest_v2,
)
from tests.backtest.test_run_input_manifest import _manifest
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


def _empty_repo(path: Path) -> BacktestRepository:
    repo = BacktestRepository(
        db.make_connect(lambda: path),
        clock=lambda: date(2026, 8, 12),
        instant_clock=lambda: NOW,
    )
    repo.ensure_schema()
    return repo


def test_backtest_schema_uses_wal_and_configures_each_connection_timeout(
    tmp_path: Path,
):
    path = tmp_path / "backtest.db"
    repo = _empty_repo(path)

    with repo._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000


def _create_bootstrap_stage_job(repo: BacktestRepository):
    """Create a lifecycle fixture without applying setup/no-op policy."""
    return repo._create_stage_job(StrategyJobType.BOOTSTRAP, None)


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


def _bootstrap_submission(
    key: str, *, parent_job_id: str | None = None
) -> BootstrapSubmissionV1:
    return BootstrapSubmissionV1(idempotency_key=key, parent_job_id=parent_job_id)


def _seed_selected_member(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO security_identities(security_id,mic,provider_symbol,evidence_digest,identity_registry_revision,created_at) VALUES('sec-001','XNAS','TEST',?,?,?)",
            ("9" * 64, "c" * 64, NOW.isoformat()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO reconstruction_roster_members(roster_digest,security_id,mic,provider_symbol,currency,source_memberships_json,identity_evidence_json,evidence_digest) VALUES(?,'sec-001','XNAS','TEST','USD','[]','{}',?)",
            (ROSTER_DIGEST, "8" * 64),
        )


def _prep(key: str) -> PreparationSubmissionV1:
    s = RunUniverseSelectionV1(
        profile_hash=PROFILE_HASH,
        activation_seq=1,
        universe_parameter="symbols",
        canonical_security_ids=("sec-001",),
        run_universe_digest=run_universe_digest(
            ["sec-001"], parameter="symbols", profile_hash=PROFILE_HASH
        ),
    )
    return PreparationSubmissionV1(
        selection=s,
        strategy_id="momentum_v1",
        strategy_api_version=1,
        strategy_source_digest=BACKTEST_STRATEGY_SOURCE_DIGEST,
        parameters={"lookback": 20, "symbols": ["sec-001"]},
        start_month="2026-05",
        end_month="2026-05",
        base_currency="USD",
        starting_capital=Decimal("10000"),
        idempotency_key=key,
    )


def _claimed_v2(repo: BacktestRepository):
    accepted = repo.create_preparation_job(_prep("seal"))
    claim = repo.claim_next_strategy_job()
    assert claim
    s = accepted.preparation.selection
    assert s
    base = _manifest(
        strategy_id="momentum_v1",
        strategy_api_version=1,
        strategy_source_digest=BACKTEST_STRATEGY_SOURCE_DIGEST,
        parameters=dict(accepted.preparation.parameters),
        profile_hash=PROFILE_HASH,
        start_month="2026-05",
        end_month="2026-05",
        ordered_month_digest=BACKTEST_ORDERED_MONTH_DIGEST,
        base_currency="USD",
        securities=(
            PinnedSecurityEvidenceV1(
                security_id="sec-001", price_revision="7" * 64, action_revision="7" * 64
            ),
        ),
    )
    m = build_run_input_manifest_v2(
        base, selection=s, source_preparation_job_id=accepted.job.id
    )
    sub = BacktestSubmissionV1(
        strategy_id=m.strategy_id,
        strategy_api_version=m.strategy_api_version,
        strategy_source_digest=m.strategy_source_digest,
        parameters=dict(m.parameters),
        profile_hash=m.profile_hash,
        start_month=m.start_month,
        end_month=m.end_month,
        base_currency=m.base_currency,
        starting_capital=m.starting_capital,
        run_input_manifest_digest=m.digest(),
        execution_contract_digest=m.execution_contract_digest(),
        canonical_manifest_json=m.canonical_json(),
        manifest_version="run_input_manifest.v2",
        universe_selection=s,
        source_preparation_job_id=accepted.job.id,
    )
    return accepted, claim, sub


def test_preparation_replay_divergence_and_deleted_target(tmp_path: Path) -> None:
    path = tmp_path / "prep.db"
    repo = _repo(path)
    _seed_selected_member(path)
    sub = _prep("k")
    a = repo.create_preparation_job(sub)
    assert repo.create_preparation_job(sub).job == a.job
    with pytest.raises(StrategyJobConflict):
        repo.create_preparation_job(
            sub.model_copy(update={"starting_capital": Decimal("2")})
        )
    claim = repo.claim_next_strategy_job()
    assert claim
    repo.request_strategy_job_cancellation(
        a.job.id, expected_version=claim.job.status_version
    )
    requested = repo.strategy_job(a.job.id)
    cancel = repo.cancel_claimed_strategy_job(
        a.job.id, claim.claim_token, expected_version=requested.status_version
    )
    repo.delete_strategy_job(a.job.id, expected_version=cancel.status_version)
    recreated = repo.create_preparation_job(sub)
    assert recreated.job.id != a.job.id


def test_legacy_upgrade_preserves_v1_and_creates_v2_index(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    repo = _repo(path)
    v1 = _enqueue_backtest(repo, idempotency_key="v1")
    assert "manifest_version" not in v1.backtest.model_dump(exclude=None)
    assert "manifest_version" not in json.loads(v1.backtest.model_dump_json())
    raw = repo.run_input_manifest_json(v1.backtest.run_input_manifest_digest)
    with sqlite3.connect(path) as c:
        c.execute("DROP TRIGGER strategy_run_v2_contract_insert")
        c.execute("DROP INDEX idx_strategy_runs_source_preparation")
        for col in (
            "selection_json",
            "source_preparation_job_id",
            "run_universe_digest",
            "manifest_version",
        ):
            c.execute(f"ALTER TABLE strategy_runs DROP COLUMN {col}")
        c.execute("ALTER TABLE run_input_manifests DROP COLUMN manifest_version")
        assert {
            str(row[1]) for row in c.execute("PRAGMA table_info(strategy_runs)")
        }.isdisjoint(
            {
                "selection_json",
                "source_preparation_job_id",
                "run_universe_digest",
                "manifest_version",
            }
        )
    reopened = BacktestRepository(db.make_connect(lambda: path))
    reopened.ensure_schema()
    assert (
        reopened.run_input_manifest_json(v1.backtest.run_input_manifest_digest) == raw
    )
    with sqlite3.connect(path) as c:
        assert c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_strategy_runs_source_preparation'"
        ).fetchone()
        index_columns = c.execute(
            "PRAGMA index_info(idx_strategy_runs_source_preparation)"
        ).fetchall()
        assert [str(row[2]) for row in index_columns] == ["source_preparation_job_id"]
        index_sql = (
            c.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='idx_strategy_runs_source_preparation'"
            )
            .fetchone()[0]
            .upper()
        )
        assert index_sql.startswith("CREATE UNIQUE INDEX")
        assert "WHERE SOURCE_PREPARATION_JOB_ID IS NOT NULL" in index_sql
        assert c.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='trigger' AND name='strategy_run_v2_contract_insert'"
        ).fetchone()
    assert reopened.strategy_run(v1.job.id).job_id == v1.job.id


def test_v2_bypass_and_mislabeled_json_rejected(tmp_path: Path) -> None:
    path = tmp_path / "gate.db"
    repo = _repo(path)
    _seed_selected_member(path)
    s = _prep("k").selection
    bad = BacktestSubmissionV1(
        strategy_id="s",
        strategy_api_version=1,
        strategy_source_digest="b" * 64,
        parameters={"symbols": ["sec-001"]},
        profile_hash=PROFILE_HASH,
        start_month="2026-05",
        end_month="2026-05",
        base_currency="USD",
        starting_capital=Decimal("1"),
        run_input_manifest_digest="c" * 64,
        execution_contract_digest="d" * 64,
        canonical_manifest_json="{}",
        manifest_version="run_input_manifest.v2",
        universe_selection=s,
        source_preparation_job_id="p",
    )
    with pytest.raises(StrategyJobConflict):
        repo.create_backtest_job(bad)
    v2 = _manifest().model_copy(update={"schema_version": "run_input_manifest.v2"})
    mislabeled = bad.model_copy(
        update={
            "manifest_version": "run_input_manifest.v1",
            "universe_selection": None,
            "source_preparation_job_id": None,
            "parent_job_id": None,
            "canonical_manifest_json": v2.model_dump_json(),
            "run_input_manifest_digest": manifest_digest(
                json.loads(v2.model_dump_json())
            ),
        }
    )
    with pytest.raises(StrategyJobConflict):
        repo.create_backtest_job(mislabeled)


@pytest.mark.parametrize(
    "field", ("parameters", "source", "period", "selection", "json", "execution")
)
def test_seal_mismatch_has_zero_mutation(tmp_path: Path, field: str) -> None:
    path = tmp_path / f"{field}.db"
    repo = _repo(path)
    _seed_selected_member(path)
    _patch_ready(repo)
    a, c, s = _claimed_v2(repo)
    if field == "parameters":
        s = s.model_copy(update={"parameters": {"symbols": ["sec-001"]}})
    elif field == "source":
        s = s.model_copy(update={"strategy_source_digest": "6" * 64})
    elif field == "period":
        s = s.model_copy(update={"end_month": "2026-06"})
    elif field == "selection":
        s = s.model_copy(
            update={
                "universe_selection": s.universe_selection.model_copy(
                    update={"activation_seq": 2}
                )
            }
        )  # type: ignore[union-attr]
    elif field == "execution":
        s = s.model_copy(update={"execution_contract_digest": "f" * 64})
    else:
        s = s.model_copy(update={"canonical_manifest_json": "{}"})
    with pytest.raises((StrategyJobConflict, ValueError)):
        repo.seal_preparation_and_create_backtest(
            a.job.id, c.claim_token, expected_version=c.job.status_version, submission=s
        )
    assert (
        len(repo.list_strategy_jobs()) == 1
        and repo.run_input_manifest_json(s.run_input_manifest_digest) is None
    )


def test_selected_only_seal_result_restart_and_retry(tmp_path: Path) -> None:
    path = tmp_path / "success.db"
    repo = _repo(path)
    _seed_selected_member(path)
    _patch_ready(repo)
    a, c, s = _claimed_v2(repo)
    child = repo.seal_preparation_and_create_backtest(
        a.job.id, c.claim_token, expected_version=c.job.status_version, submission=s
    )
    assert (
        repo.seal_preparation_and_create_backtest(
            a.job.id, c.claim_token, expected_version=c.job.status_version, submission=s
        )
        == child
    )
    assert repo.strategy_run(child.job.id).universe_selection == s.universe_selection
    cancel = repo.request_strategy_job_cancellation(
        child.job.id, expected_version=child.job.status_version
    )
    restart = repo.restart_backtest_job(
        child.job.id, expected_version=cancel.status_version, idempotency_key="r"
    )
    assert (
        restart.backtest.manifest_version == "run_input_manifest.v2"
        and restart.backtest.universe_selection == s.universe_selection
        and restart.backtest.source_preparation_job_id is None
    )


def test_malformed_preparation_and_run_selection_are_wrapped(tmp_path: Path) -> None:
    path = tmp_path / "bad-store.db"
    repo = _repo(path)
    _seed_selected_member(path)
    a = repo.create_preparation_job(_prep("k"))
    with sqlite3.connect(path) as c:
        c.execute("DROP TRIGGER preparation_run_immutable")
        c.execute(
            "UPDATE preparation_runs SET selection_json='{}' WHERE job_id=?",
            (a.job.id,),
        )
    with pytest.raises(BacktestIntegrityError):
        repo.preparation_run(a.job.id)


def test_v2_empty_stored_manifest_cannot_bypass_integrity(tmp_path: Path) -> None:
    path = tmp_path / "empty-v2.db"
    repo = _repo(path)
    _seed_selected_member(path)
    _patch_ready(repo)
    a, c, submission = _claimed_v2(repo)
    child = repo.seal_preparation_and_create_backtest(
        a.job.id,
        c.claim_token,
        expected_version=c.job.status_version,
        submission=submission,
    )
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER run_input_manifest_immutable_update")
        conn.execute(
            "UPDATE run_input_manifests SET canonical_manifest_json='{}' WHERE digest=?",
            (child.backtest.run_input_manifest_digest,),
        )
    with pytest.raises(BacktestIntegrityError, match="manifest"):
        repo.strategy_run(child.job.id)


def test_v2_result_comparison_and_malformed_run_provenance(tmp_path: Path) -> None:
    path = tmp_path / "result.db"
    repo = _repo(path)
    _seed_selected_member(path)
    _patch_ready(repo)
    a, c, s = _claimed_v2(repo)
    v2 = repo.seal_preparation_and_create_backtest(
        a.job.id, c.claim_token, expected_version=c.job.status_version, submission=s
    )
    _complete_backtest(repo, v2.job.id)
    result = repo.backtest_result(v2.job.id)
    assert (
        result.universe_selection == s.universe_selection
        and result.source_preparation_job_id == a.job.id
    )
    v1 = _enqueue_backtest(repo, idempotency_key="v1-peer")
    _complete_backtest(repo, v1.job.id)
    assert (
        repo.is_comparable(v2.job.id, v1.job.id).reason.value
        == "manifest_version_mismatch"
    )
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER strategy_run_immutable_update")
        conn.execute(
            "UPDATE strategy_runs SET selection_json='{}' WHERE id=?", (v2.job.id,)
        )
    with pytest.raises(BacktestIntegrityError):
        repo.strategy_run(v2.job.id)


def test_create_bootstrap_job_replays_the_same_persisted_job_at_every_status(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    submission = _bootstrap_submission("bootstrap-replay")
    first = repo.create_bootstrap_job(submission)
    assert first.job is not None
    job = first.job

    assert repo.create_bootstrap_job(submission).job == job
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    assert repo.create_bootstrap_job(submission).job.status is StrategyJobStatus.RUNNING
    complete = repo.complete_claimed_stage_job(
        job.id, claim.claim_token, expected_version=claim.job.status_version
    )
    assert complete.status is StrategyJobStatus.COMPLETE
    assert repo.create_bootstrap_job(submission).job == complete
    assert len(repo.list_strategy_jobs()) == 1


def test_create_bootstrap_job_returns_typed_active_profile_no_op(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")

    result = repo.create_bootstrap_job(_bootstrap_submission("active-no-op"))

    assert result.no_op is True
    assert result.job is None
    assert result.bootstrap is None
    assert repo.list_strategy_jobs() == ()


def test_create_bootstrap_job_replays_failed_and_cancelled_jobs(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")

    failed_submission = _bootstrap_submission("bootstrap-failed")
    failed_result = repo.create_bootstrap_job(failed_submission)
    assert failed_result.job is not None
    failed = failed_result.job
    failed_claim = repo.claim_next_strategy_job()
    assert failed_claim is not None
    failed_terminal = _fail_claimed(
        repo,
        failed.id,
        failed_claim.claim_token,
        expected_version=failed_claim.job.status_version,
    )
    assert failed_terminal.status is StrategyJobStatus.FAILED
    assert repo.create_bootstrap_job(failed_submission).job == failed_terminal

    cancelled_submission = _bootstrap_submission("bootstrap-cancelled")
    cancelled_result = repo.create_bootstrap_job(cancelled_submission)
    assert cancelled_result.job is not None
    cancelled = cancelled_result.job
    cancelled_claim = repo.claim_next_strategy_job()
    assert cancelled_claim is not None
    requested = repo.request_strategy_job_cancellation(
        cancelled.id, expected_version=cancelled_claim.job.status_version
    )
    cancelled_terminal = repo.cancel_claimed_strategy_job(
        cancelled.id,
        cancelled_claim.claim_token,
        expected_version=requested.status_version,
    )
    assert cancelled_terminal.status is StrategyJobStatus.CANCELLED
    assert repo.create_bootstrap_job(cancelled_submission).job == cancelled_terminal


def test_create_bootstrap_job_replays_after_reopen_with_matching_subtype_and_outbox(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    submission = _bootstrap_submission("bootstrap-reopen")
    first = _empty_repo(path).create_bootstrap_job(submission)
    assert first.job is not None
    job = first.job

    replay = _repo(path).create_bootstrap_job(submission)

    assert replay.job == job
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT job_id FROM bootstrap_runs WHERE job_id=?", (job.id,)
        ).fetchone() == (job.id,)
        assert conn.execute(
            "SELECT job_id FROM notification_outbox WHERE job_id=?", (job.id,)
        ).fetchone() == (job.id,)


def test_bootstrap_submission_rejects_whitespace_key_and_database_binding(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        _bootstrap_submission("   ")

    path = tmp_path / "backtest.db"
    repo = _empty_repo(path)
    result = repo.create_bootstrap_job(_bootstrap_submission("bootstrap-check"))
    assert result.job is not None
    job = result.job
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO bootstrap_enqueue_actions
               (idempotency_key, job_id, submission_digest, created_at)
               VALUES ('   ', ?, ?, ?)""",
            (job.id, "a" * 64, NOW.isoformat()),
        )


def test_create_bootstrap_job_rejects_divergent_reuse_without_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _empty_repo(path)
    first_result = repo.create_bootstrap_job(_bootstrap_submission("bootstrap-reuse"))
    assert first_result.job is not None
    first = first_result.job

    with pytest.raises(StrategyJobConflict, match="idempotency key"):
        repo.create_bootstrap_job(
            _bootstrap_submission("bootstrap-reuse", parent_job_id=first.id)
        )

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_jobs").fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM bootstrap_enqueue_actions"
        ).fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone() == (
            1,
        )


def test_create_bootstrap_job_rejects_a_distinct_active_submission(
    tmp_path: Path,
) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    repo.create_bootstrap_job(_bootstrap_submission("bootstrap-first"))

    with pytest.raises(StrategyJobConflict, match="already queued or running"):
        repo.create_bootstrap_job(_bootstrap_submission("bootstrap-second"))

    assert len(repo.list_strategy_jobs()) == 1


def test_create_bootstrap_job_same_key_is_atomic_across_repository_writers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    _empty_repo(path)
    submission = _bootstrap_submission("bootstrap-concurrent")
    barrier = Barrier(2)

    def submit(_index: int):
        worker_repo = _empty_repo(path)
        barrier.wait()
        return worker_repo.create_bootstrap_job(submission)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))

    assert results[0].job == results[1].job
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_jobs").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM bootstrap_runs").fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM bootstrap_enqueue_actions"
        ).fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone() == (
            1,
        )
    assert _repo(path).create_bootstrap_job(submission).job == results[0].job


def test_bootstrap_enqueue_binding_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _empty_repo(path)
    result = repo.create_bootstrap_job(_bootstrap_submission("immutable-binding"))
    assert result.job is not None

    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                """UPDATE bootstrap_enqueue_actions
                   SET submission_digest=? WHERE idempotency_key=?""",
                ("f" * 64, "immutable-binding"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM bootstrap_enqueue_actions WHERE idempotency_key=?",
                ("immutable-binding",),
            )


def test_deleted_bootstrap_submission_replay_fails_safely(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "backtest.db")
    submission = _bootstrap_submission("deleted-replay")
    result = repo.create_bootstrap_job(submission)
    assert result.job is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    failed = _fail_claimed(
        repo,
        result.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
    )
    repo.delete_strategy_job(result.job.id, expected_version=failed.status_version)

    with pytest.raises(StrategyJobConflict, match="no longer available"):
        repo.create_bootstrap_job(submission)


def test_bootstrap_enqueue_rolls_back_all_records_when_binding_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _empty_repo(path)
    original = repo._upsert_notification_outbox_on_connection

    def fail_outbox(*args, **kwargs):
        raise sqlite3.IntegrityError("forced outbox failure")

    monkeypatch.setattr(repo, "_upsert_notification_outbox_on_connection", fail_outbox)
    with pytest.raises(StrategyJobConflict, match="bootstrap job creation conflicted"):
        repo.create_bootstrap_job(_bootstrap_submission("bootstrap-rollback"))
    monkeypatch.setattr(repo, "_upsert_notification_outbox_on_connection", original)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_jobs").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM bootstrap_runs").fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM bootstrap_enqueue_actions"
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone() == (
            0,
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
    job_type: str = "backtest",
) -> None:
    """Insert a minimal ``strategy_jobs`` row directly -- used only to
    construct integrity scenarios no legitimate write path can produce
    (a status/Result-cardinality mismatch that
    ``complete_claimed_backtest_job`` could never write, or a
    subtype-less stage job)."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                job_type,
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


def test_list_backtest_activities_exposes_universe_selection_fields(
    tmp_path: Path,
) -> None:
    """gh-434: a v2 run's summary carries its pinned ``profile_hash``, the
    canonical security IDs parsed from ``selection_json``, and the
    tuning-parameters dict with the run's own universe key removed."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_selected_member(path)
    _patch_ready(repo)
    accepted, claim, submission = _claimed_v2(repo)
    child = repo.seal_preparation_and_create_backtest(
        accepted.job.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        submission=submission,
    )
    _complete_backtest(repo, child.job.id)

    activities = repo.list_backtest_activities()

    assert len(activities) == 1
    activity = activities[0]
    assert activity.profile_hash == PROFILE_HASH
    assert activity.universe_security_ids == ("sec-001",)
    assert activity.tuning_parameters == {"lookback": 20}
    # ``_parameter_summary``'s contract is unchanged: the universe key
    # stays in the persisted-parameters summary (gh-434 boundary).
    assert "symbols=['sec-001']" in activity.parameter_summary


def test_list_backtest_activities_legacy_null_selection_degrades(
    tmp_path: Path,
) -> None:
    """gh-434: a legacy run whose ``selection_json`` is NULL yields no
    universe IDs and default-key tuning-parameter filtering, never an
    error."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue_backtest(repo)

    activities = repo.list_backtest_activities()

    assert activities[0].universe_security_ids is None
    assert activities[0].tuning_parameters == {"lookback": 20}
    assert activities[0].profile_hash == PROFILE_HASH


def test_list_backtest_activities_unparseable_selection_json_degrades(
    tmp_path: Path,
) -> None:
    """gh-434: stored ``selection_json`` that no longer validates degrades
    to no universe IDs instead of failing the whole list."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue_backtest(repo)
    # The corrupt row models a legacy/foreign writer; the immutable-update
    # trigger must be bypassed to seed it (same precedent as
    # test_scan_reconstruction_cache.py).
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER strategy_run_immutable_update")
        conn.execute("UPDATE strategy_runs SET selection_json='{not json}'")

    activities = repo.list_backtest_activities()

    assert activities[0].universe_security_ids is None
    assert activities[0].tuning_parameters == {"lookback": 20}


def test_list_backtest_activities_tuning_parameters_exclude_universe_keys(
    tmp_path: Path,
) -> None:
    """gh-434: both the default universe key and the legacy
    ``selected_securities`` alias are excluded from the tuning-parameters
    dict, while ``_parameter_summary`` still renders them unchanged."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue_backtest(repo)
    # Legacy rows carrying the universe keys predate the immutable-update
    # trigger's contract; drop it to seed the historical shape.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER strategy_run_immutable_update")
        conn.execute(
            "UPDATE strategy_runs SET parameters_json=?",
            (
                json.dumps(
                    {
                        "lookback": 20,
                        "security_ids": ["sid-1"],
                        "selected_securities": ["sid-2"],
                    }
                ),
            ),
        )

    activities = repo.list_backtest_activities()

    assert activities[0].tuning_parameters == {"lookback": 20}
    assert activities[0].parameter_summary == (
        "lookback=20, security_ids=['sid-1'], selected_securities=['sid-2']"
    )


def test_list_backtest_activities_empty_parameters_render_as_defaults(
    tmp_path: Path,
) -> None:
    """gh-434 review patch: ``tuning_parameters`` is ``None`` (rendered as
    "(defaults)") only when the run had no parameters at all."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue_backtest(repo)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER strategy_run_immutable_update")
        conn.execute("UPDATE strategy_runs SET parameters_json='{}'")

    activities = repo.list_backtest_activities()

    assert activities[0].tuning_parameters is None
    assert activities[0].parameter_summary == "(defaults)"


def test_list_backtest_activities_universe_only_parameters_render_distinctly(
    tmp_path: Path,
) -> None:
    """gh-434 review patch: a run whose only parameter was the universe
    key strips to an empty dict -- not "(defaults)" -- so the template can
    say "(universe selection only)" truthfully."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _enqueue_backtest(repo)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER strategy_run_immutable_update")
        conn.execute(
            "UPDATE strategy_runs SET parameters_json=?",
            (json.dumps({"security_ids": ["sid-1"]}),),
        )

    activities = repo.list_backtest_activities()

    assert activities[0].tuning_parameters == {}
    assert activities[0].parameter_summary == "security_ids=['sid-1']"


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


# ---------------------------------------------------------------------------
# Story 4.1: four-activity schema + singleton worker lease.
# ---------------------------------------------------------------------------


LEASE_TTL = 30.0
INSTANCE_A = "worker-a"
INSTANCE_B = "worker-b"


class _MovableClock:
    """An instant clock the lease's expiry-driven takeovers can advance."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _leased_repo(path: Path, clock: _MovableClock) -> BacktestRepository:
    """Build a repository whose instant clock the lease tests control."""
    repo = BacktestRepository(
        db.make_connect(lambda: path),
        clock=lambda: date(2026, 8, 12),
        instant_clock=clock,
    )
    repo.ensure_schema()
    _seed_profile(path)
    return repo


def _fail_claimed(
    repo: BacktestRepository,
    job_id: str,
    claim_token: str,
    *,
    expected_version: int,
    lease: WorkerLeaseFenceV1 | None = None,
):
    """Free the single running slot so the next FIFO claim can proceed."""
    return repo.fail_claimed_strategy_job(
        job_id,
        claim_token,
        expected_version=expected_version,
        failure_code=JobFailureCode.WORKER_INTERRUPTED,
        failed_month=None,
        detail="freed for the next claim",
        lease=lease,
    )


def test_ensure_schema_is_repeatable_and_creates_the_four_activity_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)

    repo.ensure_schema()

    with sqlite3.connect(path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO strategy_jobs (
                       id, job_type, status, enqueue_seq, status_version,
                       created_at, updated_at
                   ) VALUES ('unknown-type', 'compaction', 'queued', 99, 1, ?, ?)""",
                (NOW.isoformat(), NOW.isoformat()),
            )
    assert {
        "bootstrap_runs",
        "initialization_runs",
        "preparation_runs",
        "strategy_runs",
        "strategy_worker_lease",
    } <= tables
    assert repo.read_worker_lease() is None


def test_each_activity_type_enqueues_with_exactly_one_subtype_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)

    bootstrap = _create_bootstrap_stage_job(repo)
    preparation = repo.create_preparation_job()

    assert bootstrap.job_type is StrategyJobType.BOOTSTRAP
    assert preparation.job_type is StrategyJobType.PREPARATION
    assert repo.bootstrap_run(bootstrap.id).job_id == bootstrap.id
    assert repo.preparation_run(preparation.id).job_id == preparation.id
    with pytest.raises(StrategyJobNotFound):
        repo.preparation_run(bootstrap.id)
    with pytest.raises(StrategyJobNotFound):
        repo.bootstrap_run(preparation.id)


def test_subtype_rows_cannot_be_duplicated_or_attached_to_another_type(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    bootstrap = _create_bootstrap_stage_job(repo)

    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO bootstrap_runs (job_id) VALUES (?)", (bootstrap.id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO preparation_runs (job_id) VALUES (?)", (bootstrap.id,)
            )


def test_claiming_a_subtype_less_stage_job_is_rejected_and_leaves_it_queued(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_bare_job(
        path, "orphan-bootstrap", status="queued", enqueue_seq=1, job_type="bootstrap"
    )

    with pytest.raises(StrategyJobConflict):
        repo.claim_next_strategy_job()

    assert repo.strategy_job("orphan-bootstrap").status is StrategyJobStatus.QUEUED


def test_claim_is_fifo_across_all_four_activity_types(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    initialization = _enqueue(repo, "2026-05", "2026-05").job
    bootstrap = _create_bootstrap_stage_job(repo)
    backtest = _enqueue_backtest(repo).job
    preparation = repo.create_preparation_job()
    assert backtest is not None and initialization is not None

    claimed: list[str] = []
    for _ in range(4):
        claim = repo.claim_next_strategy_job()
        assert claim is not None
        claimed.append(claim.job.id)
        _fail_claimed(
            repo,
            claim.job.id,
            claim.claim_token,
            expected_version=claim.job.status_version,
        )

    assert claimed == [initialization.id, bootstrap.id, backtest.id, preparation.id]
    assert repo.claim_next_strategy_job() is None


def test_claim_carries_only_its_own_subtype_for_each_stage_type(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    bootstrap = _create_bootstrap_stage_job(repo)

    claim = repo.claim_next_strategy_job()

    assert claim is not None and claim.job.id == bootstrap.id
    assert claim.bootstrap is not None and claim.bootstrap.job_id == bootstrap.id
    assert claim.preparation is None
    assert claim.initialization is None
    assert claim.backtest is None


def test_stage_walk_advances_a_closed_stage_sequence_then_completes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    preparation = repo.create_preparation_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    job = claim.job
    for stage in PreparationStage:
        job = repo.set_strategy_job_current_stage(
            preparation.id,
            claim.claim_token,
            expected_version=job.status_version,
            stage=stage.value,
        )
        assert job.current_stage == stage.value
        assert job.current_month is None

    completed = repo.complete_claimed_stage_job(
        preparation.id, claim.claim_token, expected_version=job.status_version
    )

    assert completed.status is StrategyJobStatus.COMPLETE
    assert completed.current_stage is None


def test_stage_and_month_progress_mechanisms_never_cross_job_types(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    bootstrap = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    with pytest.raises(StrategyJobConflict):
        repo.set_strategy_job_current_month(
            bootstrap.id,
            claim.claim_token,
            expected_version=claim.job.status_version,
            month="2026-05",
        )
    with pytest.raises(StrategyJobConflict):
        repo.set_strategy_job_current_stage(
            bootstrap.id,
            claim.claim_token,
            expected_version=claim.job.status_version,
            stage=PreparationStage.FX_PINNING.value,
        )
    assert repo.strategy_job(bootstrap.id).current_stage is None


def test_month_typed_job_cannot_report_a_stage(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    initialization = _enqueue(repo, "2026-05", "2026-05").job
    assert initialization is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    with pytest.raises(StrategyJobConflict):
        repo.set_strategy_job_current_stage(
            initialization.id,
            claim.claim_token,
            expected_version=claim.job.status_version,
            stage=BootstrapStage.QUALIFICATION.value,
        )


def test_first_lease_starts_at_generation_one_and_renewal_keeps_it(
    tmp_path: Path,
) -> None:
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)

    acquired = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)
    clock.advance(1)
    renewed = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)

    assert acquired.generation == 1
    assert renewed.generation == 1
    assert renewed.instance_id == INSTANCE_A
    assert renewed.expires_at > acquired.expires_at


def test_live_lease_is_never_taken_over_by_a_second_instance(tmp_path: Path) -> None:
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)
    repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)

    clock.advance(LEASE_TTL - 1)
    with pytest.raises(StrategyJobConflict):
        repo.acquire_or_renew_worker_lease(INSTANCE_B, ttl_seconds=LEASE_TTL)

    held = repo.read_worker_lease()
    assert held is not None and held.instance_id == INSTANCE_A


def test_expired_lease_is_taken_over_at_the_next_generation(tmp_path: Path) -> None:
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)
    first = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)

    clock.advance(LEASE_TTL + 1)
    second = repo.acquire_or_renew_worker_lease(INSTANCE_B, ttl_seconds=LEASE_TTL)

    assert second.generation == first.generation + 1
    assert second.instance_id == INSTANCE_B


def test_reading_the_lease_never_renews_or_bumps_it(tmp_path: Path) -> None:
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)
    acquired = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)

    clock.advance(LEASE_TTL + 1)
    assert repo.read_worker_lease() == acquired
    assert repo.read_worker_lease() == acquired


def test_stale_generation_writer_is_rejected_without_mutating_the_job(
    tmp_path: Path,
) -> None:
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)
    _seed_profile(tmp_path / "backtest.db")
    first = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)
    bootstrap = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job(lease=first.fence)
    assert claim is not None and claim.lease_generation == first.generation

    clock.advance(LEASE_TTL + 1)
    second = repo.acquire_or_renew_worker_lease(INSTANCE_B, ttl_seconds=LEASE_TTL)
    before = repo.strategy_job(bootstrap.id)

    with pytest.raises(StrategyJobConflict):
        repo.set_strategy_job_current_stage(
            bootstrap.id,
            claim.claim_token,
            expected_version=before.status_version,
            stage=BootstrapStage.QUALIFICATION.value,
            lease=first.fence,
        )
    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_stage_job(
            bootstrap.id,
            claim.claim_token,
            expected_version=before.status_version,
            lease=first.fence,
        )
    with pytest.raises(StrategyJobConflict):
        _fail_claimed(
            repo,
            bootstrap.id,
            claim.claim_token,
            expected_version=before.status_version,
            lease=first.fence,
        )

    assert repo.strategy_job(bootstrap.id) == before
    assert second.generation == first.generation + 1


def test_current_generation_writer_still_owns_the_job_after_a_takeover_attempt(
    tmp_path: Path,
) -> None:
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)
    lease = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)
    bootstrap = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job(lease=lease.fence)
    assert claim is not None

    clock.advance(1)
    with pytest.raises(StrategyJobConflict):
        repo.acquire_or_renew_worker_lease(INSTANCE_B, ttl_seconds=LEASE_TTL)
    completed = repo.complete_claimed_stage_job(
        bootstrap.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        lease=lease.fence,
    )

    assert completed.status is StrategyJobStatus.COMPLETE
    assert completed.owner_instance_id is None
    assert completed.lease_generation is None


def test_unfenced_writer_is_rejected_once_a_lease_is_held(tmp_path: Path) -> None:
    clock = _MovableClock()
    repo = _leased_repo(tmp_path / "backtest.db", clock)
    lease = repo.acquire_or_renew_worker_lease(INSTANCE_A, ttl_seconds=LEASE_TTL)
    bootstrap = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job(lease=lease.fence)
    assert claim is not None

    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_stage_job(
            bootstrap.id,
            claim.claim_token,
            expected_version=claim.job.status_version,
        )

    assert repo.strategy_job(bootstrap.id).status is StrategyJobStatus.RUNNING


def test_terminal_completion_that_commits_first_wins_the_cancellation_race(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    bootstrap = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    completed = repo.complete_claimed_stage_job(
        bootstrap.id, claim.claim_token, expected_version=claim.job.status_version
    )
    late = repo.request_strategy_job_cancellation(
        bootstrap.id, expected_version=completed.status_version
    )

    assert late == completed
    assert late.status is StrategyJobStatus.COMPLETE


def test_cancellation_requested_before_completion_blocks_the_terminal_write(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    bootstrap = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    requested = repo.request_strategy_job_cancellation(
        bootstrap.id, expected_version=claim.job.status_version
    )

    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_stage_job(
            bootstrap.id, claim.claim_token, expected_version=requested.status_version
        )
    cancelled = repo.cancel_claimed_strategy_job(
        bootstrap.id, claim.claim_token, expected_version=requested.status_version
    )

    assert cancelled.status is StrategyJobStatus.CANCELLED


def test_stage_job_terminal_states_offer_delete_but_never_restart(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    preparation = repo.create_preparation_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    failed = _fail_claimed(
        repo,
        preparation.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
    )

    assert failed.failure_code is JobFailureCode.WORKER_INTERRUPTED
    assert repo.legal_strategy_job_actions(preparation.id) == ("delete",)


def test_profile_activation_does_not_offer_bootstrap_cancellation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    bootstrap = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    active = repo.set_strategy_job_current_stage(
        bootstrap.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        stage=BootstrapStage.PROFILE_ACTIVATION.value,
    )

    assert active.status is StrategyJobStatus.RUNNING
    assert repo.legal_strategy_job_actions(bootstrap.id) == ()


def test_manifest_sealing_does_not_offer_preparation_cancellation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    preparation = repo.create_preparation_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    sealing = repo.set_strategy_job_current_stage(
        preparation.id,
        claim.claim_token,
        expected_version=claim.job.status_version,
        stage=PreparationStage.MANIFEST_SEALING.value,
    )

    assert sealing.status is StrategyJobStatus.RUNNING
    assert repo.legal_strategy_job_actions(preparation.id) == ()
    assert (
        repo.request_strategy_job_cancellation(
            preparation.id, expected_version=sealing.status_version
        )
        == sealing
    )


def test_deleting_a_stage_job_tombstones_it_and_drops_its_subtype_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    bootstrap = _create_bootstrap_stage_job(repo)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    failed = _fail_claimed(
        repo, bootstrap.id, claim.claim_token, expected_version=claim.job.status_version
    )

    tombstone = repo.delete_strategy_job(
        bootstrap.id, expected_version=failed.status_version
    )

    assert tombstone.deleted_at is not None
    assert tombstone.audit_summary is not None
    with pytest.raises(StrategyJobNotFound):
        repo.bootstrap_run(bootstrap.id)


def test_stage_check_constraint_mirrors_the_declared_stage_enums(
    tmp_path: Path,
) -> None:
    """Guard the one place the closed stage set is written twice: the
    ``current_stage`` CHECK and the two ``StrategyJobType`` stage enums."""
    path = tmp_path / "backtest.db"
    _repo(path)

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategy_jobs'"
        ).fetchone()
    ddl = str(row[0])

    for stage in STAGE_VALUES:
        assert f"'{stage}'" in ddl
    assert STAGE_VALUES == tuple(BootstrapStage) + tuple(PreparationStage)
