"""Story 3.1 coverage: the one canonical comparison-eligibility predicate
-- ``BacktestRepository.is_comparable``/``comparison_candidates`` --
exercised against every row of the spec's I/O & Edge-Case Matrix via real
on-disk SQLite, reusing ``test_backtest_repo_results.py``'s
``_repo``/``_seed_profile`` fixture pattern (no mocks)."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.repositories.backtest_repo import (
    BacktestIntegrityError,
    BacktestRepository,
    ComparisonEligibilityV1,
    ComparisonIneligibleReason,
)
from app.services.backtest.strategy_job import StrategyJobNotFound
from app.services.backtest.trading_calendar import TradingCalendar

from tests.backtest.test_backtest_repo_results import (
    CLAIM_TOKEN,
    EXECUTION_CONTRACT_DIGEST,
    MANIFEST_DIGEST,
    NOW,
    ORDERED_MONTH_DIGEST,
    PROFILE_HASH,
    ROSTER_DIGEST,
    STRATEGY_SOURCE_DIGEST,
    _repo,
    _seed_profile,
    _seed_run_input_manifest,
    _staging_payload,
)

ANCHOR_ID = "anchor-run"
OTHER_ID = "other-run"


def _seed_snapshot_profile(path: Path, profile_hash: str) -> None:
    """Seed an additional ``snapshot_profiles`` row for ``profile_hash``,
    reusing the roster ``_seed_profile`` already seeds -- lets mismatch
    tests vary ``profile_hash`` independently of the shared default."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT OR IGNORE INTO snapshot_profiles
               (profile_hash, canonical_profile_json, display_version, roster_digest,
                scanner_schema_version, calendar_dataset_version,
                calendar_dataset_digest, cadence)
               VALUES (?, '{}', 'Scanner data v1', ?, 'historical_scan_record.v1',
                       'exchange-calendars-v1', ?, 'per-exchange month_end')""",
            (profile_hash, ROSTER_DIGEST, TradingCalendar().session_table_digest()),
        )


def _complete_run(
    path: Path,
    repo: BacktestRepository,
    *,
    run_id: str,
    enqueue_seq: int,
    strategy_id: str = "momentum_v1",
    parameters_json: str = '{"lookback": 20}',
    profile_hash: str = PROFILE_HASH,
    start_month: str = "2026-01",
    end_month: str = "2026-01",
    ordered_month_digest: str = ORDERED_MONTH_DIGEST,
    base_currency: str = "USD",
    starting_capital: str = "10000.00000000",
    execution_contract_digest: str = EXECUTION_CONTRACT_DIGEST,
) -> None:
    """Seed one fully complete Backtest job + Result with independently
    controllable comparison-dimension fields, going through the real
    staging-write-then-complete API path -- mirroring
    ``test_backtest_repo_results.py``'s completion tests -- so the stored
    ``result_digest`` is always genuinely valid rather than hand-crafted."""
    _seed_profile(path)
    _seed_snapshot_profile(path, profile_hash)
    _seed_run_input_manifest(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, claim_token, current_month,
                   status_version, cancel_requested_at, created_at, updated_at
               ) VALUES (?, 'backtest', 'running', ?, ?, NULL, 1, NULL, ?, ?)""",
            (run_id, enqueue_seq, CLAIM_TOKEN, NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            """INSERT INTO strategy_runs (
                   id, strategy_id, strategy_api_version, strategy_source_digest,
                   parameters_json, profile_hash, start_month, end_month,
                   ordered_month_digest, base_currency, starting_capital,
                   run_input_manifest_digest, execution_contract_digest, created_at
               ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                strategy_id,
                STRATEGY_SOURCE_DIGEST,
                parameters_json,
                profile_hash,
                start_month,
                end_month,
                ordered_month_digest,
                base_currency,
                starting_capital,
                MANIFEST_DIGEST,
                execution_contract_digest,
                NOW.isoformat(),
            ),
        )
    repo.write_backtest_staging(
        run_id,
        claim_token=CLAIM_TOKEN,
        expected_version=1,
        **_staging_payload(),  # type: ignore[arg-type]
    )
    repo.complete_claimed_backtest_job(run_id, CLAIM_TOKEN, expected_version=1)


def _seed_incomplete_job(
    path: Path, *, run_id: str, status: str, enqueue_seq: int
) -> None:
    """Seed a bare ``strategy_jobs`` row (no pinned ``strategy_runs``
    identity) in a non-complete status -- mirrors the FIFO-placeholder
    shape Story 2.2/2.3 already accepts for a Backtest job before, or
    without, a completed attempt."""
    claim_token = CLAIM_TOKEN if status == "running" else None
    cancel_requested_at = NOW.isoformat() if status == "cancelled" else None
    failure_code = "required_data_missing" if status == "failed" else None
    failure_detail = "seeded failure" if status == "failed" else None
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, claim_token, current_month,
                   status_version, cancel_requested_at, failure_code, failure_detail,
                   created_at, updated_at
               ) VALUES (?, 'backtest', ?, ?, ?, NULL, 1, ?, ?, ?, ?, ?)""",
            (
                run_id,
                status,
                enqueue_seq,
                claim_token,
                cancel_requested_at,
                failure_code,
                failure_detail,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


def _tombstone(path: Path, run_id: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE strategy_jobs
               SET deleted_at=?, status_version=status_version+1 WHERE id=?""",
            (NOW.isoformat(), run_id),
        )


# ---------------------------------------------------------------------------
# is_comparable: self-comparison, missing, tombstoned, non-complete
# ---------------------------------------------------------------------------


def test_self_comparison_is_rejected_without_a_db_lookup(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)

    eligibility = repo.is_comparable("does-not-exist", "does-not-exist")

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.SELF_COMPARISON


def test_missing_left_is_not_found(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=1)

    eligibility = repo.is_comparable("does-not-exist", OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.NOT_FOUND


def test_missing_right_is_not_found(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)

    eligibility = repo.is_comparable(ANCHOR_ID, "does-not-exist")

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.NOT_FOUND


def test_tombstoned_left_is_ineligible(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    _tombstone(path, ANCHOR_ID)

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.TOMBSTONED


def test_tombstoned_right_is_ineligible(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    _tombstone(path, OTHER_ID)

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.TOMBSTONED


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_non_complete_status_is_ineligible(tmp_path: Path, status: str) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _seed_incomplete_job(path, run_id=OTHER_ID, status=status, enqueue_seq=2)

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.NOT_COMPLETE


def test_complete_non_backtest_job_type_is_not_complete(tmp_path: Path) -> None:
    """A completed Initialization job's id is never comparable, even
    though its ``status`` is ``complete`` -- isolates the ``job_type``
    half of the ``NOT_COMPLETE`` check from the ``status`` half."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES (?, 'initialization', 'complete', 2, 1, ?, ?)""",
            (OTHER_ID, NOW.isoformat(), NOW.isoformat()),
        )

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.NOT_COMPLETE


# ---------------------------------------------------------------------------
# is_comparable: each mismatch dimension in isolation
# ---------------------------------------------------------------------------


def test_period_start_mismatch_is_ineligible(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(
        path,
        repo,
        run_id=ANCHOR_ID,
        enqueue_seq=1,
        start_month="2026-01",
        end_month="2026-02",
    )
    _complete_run(
        path,
        repo,
        run_id=OTHER_ID,
        enqueue_seq=2,
        start_month="2026-02",
        end_month="2026-02",
    )

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.PERIOD_MISMATCH
    assert "start_month" in eligibility.detail


def test_period_end_mismatch_is_ineligible(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(
        path,
        repo,
        run_id=ANCHOR_ID,
        enqueue_seq=1,
        start_month="2026-01",
        end_month="2026-01",
    )
    _complete_run(
        path,
        repo,
        run_id=OTHER_ID,
        enqueue_seq=2,
        start_month="2026-01",
        end_month="2026-02",
    )

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.PERIOD_MISMATCH
    assert "end_month" in eligibility.detail


def test_profile_hash_mismatch_is_ineligible(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    other_profile_hash = "9" * 64
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(
        path, repo, run_id=OTHER_ID, enqueue_seq=2, profile_hash=other_profile_hash
    )

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.PROFILE_MISMATCH
    assert "profile_hash" in eligibility.detail


def test_ordered_month_digest_mismatch_is_ineligible(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    other_digest = "7" * 64
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(
        path, repo, run_id=OTHER_ID, enqueue_seq=2, ordered_month_digest=other_digest
    )

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.EVIDENCE_DIGEST_MISMATCH
    assert "ordered_month_digest" in eligibility.detail


def test_base_currency_mismatch_is_ineligible(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1, base_currency="USD")
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2, base_currency="GBP")

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.CURRENCY_MISMATCH
    assert "base_currency" in eligibility.detail


def test_execution_contract_digest_mismatch_is_ineligible(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    other_digest = "5" * 64
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(
        path,
        repo,
        run_id=OTHER_ID,
        enqueue_seq=2,
        execution_contract_digest=other_digest,
    )

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible is False
    assert eligibility.reason is ComparisonIneligibleReason.EXECUTION_CONTRACT_MISMATCH
    assert "execution_contract_digest" in eligibility.detail


# ---------------------------------------------------------------------------
# is_comparable: non-compared dimensions may differ freely
# ---------------------------------------------------------------------------


def test_differing_strategy_parameters_and_capital_alone_stay_eligible(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(
        path,
        repo,
        run_id=ANCHOR_ID,
        enqueue_seq=1,
        strategy_id="momentum_v1",
        parameters_json='{"lookback": 20}',
        starting_capital="10000.00000000",
    )
    _complete_run(
        path,
        repo,
        run_id=OTHER_ID,
        enqueue_seq=2,
        strategy_id="mean_reversion_v2",
        parameters_json='{"lookback": 40, "threshold": 2}',
        starting_capital="25000.00000000",
    )

    eligibility = repo.is_comparable(ANCHOR_ID, OTHER_ID)

    assert eligibility == ComparisonEligibilityV1(eligible=True, reason=None, detail="")


# ---------------------------------------------------------------------------
# is_comparable: the integrity boundary (Story 2.5's, not a new reason)
# ---------------------------------------------------------------------------


def test_complete_job_with_vanished_result_raises_not_found(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    # A ``complete`` job whose Result row is somehow absent violates the
    # cardinality invariant ``list_backtest_activities`` already enforces
    # -- unreachable through normal completion (the terminal-immutable
    # trigger blocks transitioning an already-terminal job), so this
    # inserts the impossible shape directly, matching this suite's other
    # deliberate-corruption tests.
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, claim_token, current_month,
                   status_version, cancel_requested_at, created_at, updated_at
               ) VALUES (?, 'backtest', 'complete', 1, NULL, NULL, 1, NULL, ?, ?)""",
            (ANCHOR_ID, NOW.isoformat(), NOW.isoformat()),
        )
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)

    with pytest.raises(StrategyJobNotFound):
        repo.is_comparable(ANCHOR_ID, OTHER_ID)


def test_complete_job_with_tampered_result_raises_and_is_not_mutated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)

    with sqlite3.connect(path) as conn:
        before_anchor = conn.execute(
            "SELECT result_digest FROM backtest_results WHERE run_id=?", (ANCHOR_ID,)
        ).fetchone()[0]
        # ``backtest_result_evidence_immutable`` blocks a normal direct
        # ``UPDATE`` -- drop it first to simulate tampered-at-rest
        # evidence, mirroring ``test_retrieval_detects_tampered_evidence``.
        conn.execute("DROP TRIGGER IF EXISTS backtest_result_evidence_immutable")
        conn.execute(
            """UPDATE backtest_results
               SET metrics_json=?, note_version=note_version+1 WHERE run_id=?""",
            (
                '{"total_return": 999.0, "sharpe_ratio": null, '
                '"win_rate": null, "max_drawdown": null}',
                OTHER_ID,
            ),
        )
        before_other = conn.execute(
            "SELECT result_digest FROM backtest_results WHERE run_id=?", (OTHER_ID,)
        ).fetchone()[0]

    with pytest.raises(BacktestIntegrityError):
        repo.is_comparable(ANCHOR_ID, OTHER_ID)

    with sqlite3.connect(path) as conn:
        after_anchor = conn.execute(
            "SELECT result_digest FROM backtest_results WHERE run_id=?", (ANCHOR_ID,)
        ).fetchone()[0]
        after_other = conn.execute(
            "SELECT result_digest FROM backtest_results WHERE run_id=?", (OTHER_ID,)
        ).fetchone()[0]
    assert after_anchor == before_anchor
    assert after_other == before_other  # tampered by test setup, not by our call


# ---------------------------------------------------------------------------
# comparison_candidates
# ---------------------------------------------------------------------------


def test_comparison_candidates_returns_only_eligible_peers_ordered_desc(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(
        path,
        repo,
        run_id="eligible-low-seq",
        enqueue_seq=2,
        strategy_id="mean_reversion_v2",
    )
    _complete_run(path, repo, run_id="mismatched", enqueue_seq=3, base_currency="GBP")
    _complete_run(path, repo, run_id="tombstoned-peer", enqueue_seq=4)
    _tombstone(path, "tombstoned-peer")
    _seed_incomplete_job(path, run_id="incomplete-peer", status="queued", enqueue_seq=6)
    _complete_run(
        path,
        repo,
        run_id="eligible-high-seq",
        enqueue_seq=5,
        parameters_json='{"lookback": 99}',
    )

    candidates = repo.comparison_candidates(ANCHOR_ID)

    assert [c.run_id for c in candidates] == [
        "eligible-high-seq",
        "eligible-low-seq",
    ]
    high = candidates[0]
    assert high.strategy_id == "momentum_v1"
    assert high.strategy_api_version == 1
    assert high.parameter_summary == "lookback=99"
    assert high.start_month == "2026-01"
    assert high.end_month == "2026-01"
    assert high.base_currency == "USD"
    assert high.profile_hash == PROFILE_HASH


def test_comparison_candidates_excludes_anchor_and_never_preselects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)

    candidates = repo.comparison_candidates(ANCHOR_ID)

    assert candidates == ()


def test_comparison_candidates_on_missing_anchor_raises(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)

    with pytest.raises(StrategyJobNotFound):
        repo.comparison_candidates("does-not-exist")


def test_comparison_candidates_on_incomplete_anchor_raises(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_incomplete_job(path, run_id=ANCHOR_ID, status="failed", enqueue_seq=1)

    with pytest.raises(StrategyJobNotFound):
        repo.comparison_candidates(ANCHOR_ID)


def test_comparison_candidates_raises_on_tampered_candidate_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS backtest_result_evidence_immutable")
        conn.execute(
            """UPDATE backtest_results
               SET metrics_json=?, note_version=note_version+1 WHERE run_id=?""",
            (
                '{"total_return": 999.0, "sharpe_ratio": null, '
                '"win_rate": null, "max_drawdown": null}',
                OTHER_ID,
            ),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.comparison_candidates(ANCHOR_ID)
