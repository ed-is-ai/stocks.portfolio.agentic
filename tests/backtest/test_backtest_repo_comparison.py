"""Story 3.1 coverage: the one canonical comparison-eligibility predicate
-- ``BacktestRepository.is_comparable``/``comparison_candidates`` --
exercised against every row of the spec's I/O & Edge-Case Matrix via real
on-disk SQLite, reusing ``test_backtest_repo_results.py``'s
``_repo``/``_seed_profile`` fixture pattern (no mocks)."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
import sqlite3
import threading

import pytest

from app.repositories.backtest_repo import (
    RESULT_VERIFIER_SOURCE_DIGEST,
    RESULT_VERIFIER_VERSION,
    BacktestIntegrityError,
    BacktestRepository,
    ComparisonEligibilityV1,
    ComparisonIneligibleReason,
)
from app.services.backtest.strategy_outcomes import StrategyOutcomeService
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


def test_recent_result_summaries_inspect_at_most_twenty_five_candidates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bounded-results.db"
    repo = _repo(path)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, claim_token, current_month,
                   status_version, cancel_requested_at, created_at, updated_at
               ) VALUES (?, 'backtest', 'complete', ?, NULL, NULL, 1, NULL, ?, ?)""",
            [
                (
                    f"missing-result-{index:02d}",
                    index + 1,
                    NOW.isoformat(),
                    NOW.isoformat(),
                )
                for index in range(30)
            ],
        )

    page = repo.recent_backtest_result_summaries(limit=25)

    assert page.inspected_count == 25
    assert page.missing_result_count == 25
    assert page.integrity_excluded_count == 0
    assert page.results == ()


def test_recent_result_summaries_count_tampered_metrics_unverified_then_failed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "damaged-result.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id="damaged-run", enqueue_seq=1)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS backtest_result_evidence_immutable")
        conn.execute(
            """UPDATE backtest_results
               SET metrics_json=?, note_version=note_version+1 WHERE run_id=?""",
            (
                '{"total_return": 99.0, "sharpe_ratio": null, '
                '"win_rate": null, "max_drawdown": null}',
                "damaged-run",
            ),
        )

    page = repo.recent_backtest_result_summaries(limit=25)

    assert page.inspected_count == 0
    assert page.unverified_count == 1
    assert page.integrity_excluded_count == 0
    assert page.missing_result_count == 0
    assert page.results == ()

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result("damaged-run")
    page = repo.recent_backtest_result_summaries(limit=25)

    assert page.unverified_count == 0
    assert page.integrity_excluded_count == 1


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


def test_comparison_candidates_sql_prefilter_loads_only_plausible_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id="eligible", enqueue_seq=20)
    _complete_run(
        path,
        repo,
        run_id="wrong-start",
        enqueue_seq=19,
        start_month="2026-02",
        end_month="2026-02",
    )
    _complete_run(path, repo, run_id="wrong-end", enqueue_seq=18, end_month="2026-02")
    _complete_run(
        path, repo, run_id="wrong-profile", enqueue_seq=17, profile_hash="9" * 64
    )
    _complete_run(
        path,
        repo,
        run_id="wrong-evidence",
        enqueue_seq=16,
        ordered_month_digest="8" * 64,
    )
    _complete_run(
        path, repo, run_id="wrong-currency", enqueue_seq=15, base_currency="GBP"
    )
    _complete_run(
        path,
        repo,
        run_id="wrong-contract",
        enqueue_seq=14,
        execution_contract_digest="7" * 64,
    )

    original_backtest_result = repo.backtest_result
    loaded_ids: list[str] = []

    def counting_backtest_result(candidate_id: str):
        loaded_ids.append(candidate_id)
        return original_backtest_result(candidate_id)

    monkeypatch.setattr(repo, "backtest_result", counting_backtest_result)

    candidates = repo.comparison_candidates(ANCHOR_ID)

    assert [candidate.run_id for candidate in candidates] == ["eligible"]
    assert loaded_ids == [ANCHOR_ID, "eligible"]


def test_comparison_candidates_reuses_freshly_verified_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    anchor = repo.backtest_result(ANCHOR_ID)

    original_backtest_result = repo.backtest_result
    loaded_ids: list[str] = []

    def counting_backtest_result(candidate_id: str):
        loaded_ids.append(candidate_id)
        return original_backtest_result(candidate_id)

    monkeypatch.setattr(repo, "backtest_result", counting_backtest_result)

    candidates = repo.comparison_candidates(ANCHOR_ID, anchor_result=anchor)

    assert [candidate.run_id for candidate in candidates] == [OTHER_ID]
    assert loaded_ids == [OTHER_ID]


def test_is_comparable_reuses_freshly_verified_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    anchor = repo.backtest_result(ANCHOR_ID)
    other = repo.backtest_result(OTHER_ID)

    original_backtest_result = repo.backtest_result
    loaded_ids: list[str] = []

    def counting_backtest_result(candidate_id: str):
        loaded_ids.append(candidate_id)
        return original_backtest_result(candidate_id)

    monkeypatch.setattr(repo, "backtest_result", counting_backtest_result)

    eligibility = repo.is_comparable(
        ANCHOR_ID,
        OTHER_ID,
        left_result=anchor,
        right_result=other,
    )

    assert eligibility.eligible
    assert loaded_ids == []


def test_comparison_results_loads_each_eligible_side_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    original_backtest_result = repo.backtest_result
    loaded_ids: list[str] = []

    def counting_backtest_result(run_id: str):
        loaded_ids.append(run_id)
        return original_backtest_result(run_id)

    monkeypatch.setattr(repo, "backtest_result", counting_backtest_result)
    eligibility, left, right = repo.comparison_results_if_eligible(ANCHOR_ID, OTHER_ID)

    assert eligibility.eligible
    assert left is not None and right is not None
    assert loaded_ids == [ANCHOR_ID, OTHER_ID]


def test_comparison_candidates_reused_anchor_still_rejects_tampered_peer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    anchor = repo.backtest_result(ANCHOR_ID)

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
        repo.comparison_candidates(ANCHOR_ID, anchor_result=anchor)


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


def test_list_flags_a_tampered_result_and_keeps_the_others(tmp_path: Path) -> None:
    """A Result failing verification is listed without Metrics, flagged."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS backtest_result_evidence_immutable")
        conn.execute(
            "UPDATE backtest_results SET metrics_json=?, "
            "note_version=note_version+1 WHERE run_id=?",
            (
                '{"total_return": 999.0, "sharpe_ratio": null, '
                '"win_rate": null, "max_drawdown": null}',
                OTHER_ID,
            ),
        )

    by_id = {a.job.id: a for a in repo.list_backtest_activities()}

    assert by_id[OTHER_ID].metrics is None
    assert by_id[OTHER_ID].result_verdict == "unverified"
    assert by_id[OTHER_ID].result_error is None
    assert by_id[ANCHOR_ID].metrics is not None
    assert by_id[ANCHOR_ID].result_verdict == "verified"
    assert by_id[ANCHOR_ID].result_error is None

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(OTHER_ID)

    verdict = _verdict(path, OTHER_ID)
    assert verdict is not None
    assert verdict[4:6] == ("failed", "stored backtest result digest is invalid")
    by_id = {a.job.id: a for a in repo.list_backtest_activities()}
    assert by_id[OTHER_ID].result_verdict == "failed"
    assert by_id[OTHER_ID].result_error == "stored backtest result digest is invalid"


# ---------------------------------------------------------------------------
# gh-133: Result verdicts and list summaries (AD-9, AD-11)
# ---------------------------------------------------------------------------


def _verdict(path: Path, run_id: str) -> tuple[object, ...] | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """SELECT result_digest, result_schema_version, verifier_version,
                      metrics_digest, status, detail
               FROM backtest_result_verdicts WHERE run_id=?""",
            (run_id,),
        ).fetchone()
    return None if row is None else tuple(row)


def _current_key(path: Path, run_id: str) -> tuple[object, ...]:
    with sqlite3.connect(path) as conn:
        digest, schema, metrics_json = conn.execute(
            """SELECT result_digest, result_schema_version, metrics_json
               FROM backtest_results WHERE run_id=?""",
            (run_id,),
        ).fetchone()
    return (
        digest,
        schema,
        RESULT_VERIFIER_VERSION,
        hashlib.sha256(metrics_json.encode("utf-8")).hexdigest(),
    )


def _execute(path: Path, *statements: str) -> None:
    with sqlite3.connect(path) as conn:
        for statement in statements:
            conn.execute(statement)


def _tamper_trade_log(path: Path, run_id: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS trade_log_immutable_update")
        conn.execute(
            """UPDATE trade_log SET event_json=replace(event_json, '"50', '"51')
               WHERE run_id=? AND kind='exit_fill'""",
            (run_id,),
        )


def _count_backtest_result_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = BacktestRepository.backtest_result

    def counting(self: BacktestRepository, run_id: str, **kwargs: bool):
        calls.append(run_id)
        return original(self, run_id, **kwargs)

    monkeypatch.setattr(BacktestRepository, "backtest_result", counting)
    return calls


def test_fresh_completion_records_a_current_verified_verdict(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)

    verdict = _verdict(path, ANCHOR_ID)
    (activity,) = repo.list_backtest_activities()

    assert verdict is not None
    assert verdict[:4] == _current_key(path, ANCHOR_ID)
    assert verdict[4:] == ("verified", None)
    assert activity.result_verdict == "verified"
    assert activity.metrics is not None
    assert activity.metric_availability is not None


def test_missing_verdict_lists_unverified_without_metrics(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _execute(path, "DELETE FROM backtest_result_verdicts")

    (activity,) = repo.list_backtest_activities()
    page = repo.recent_backtest_result_summaries(limit=25)

    assert activity.result_verdict == "unverified"
    assert activity.metrics is None
    assert activity.result_error is None
    assert page.results == ()
    assert page.unverified_count == 1


def test_bumped_verifier_is_unverified_until_open_rewrites_it(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _execute(path, "UPDATE backtest_result_verdicts SET verifier_version='old.v0'")

    assert repo.list_backtest_activities()[0].result_verdict == "unverified"

    repo.backtest_result(ANCHOR_ID)

    verdict = _verdict(path, ANCHOR_ID)
    assert verdict is not None
    assert verdict[:4] == _current_key(path, ANCHOR_ID)
    assert repo.list_backtest_activities()[0].result_verdict == "verified"


def test_tampered_evidence_stays_verified_until_opened(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _tamper_trade_log(path, ANCHOR_ID)

    assert repo.list_backtest_activities()[0].result_verdict == "verified"

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(ANCHOR_ID)

    (activity,) = repo.list_backtest_activities()
    assert activity.result_verdict == "failed"
    assert activity.metrics is None
    assert activity.result_error is not None
    assert repo.recent_backtest_result_summaries().integrity_excluded_count == 1


def test_failed_verdict_still_runs_full_verification_on_open(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _execute(
        path,
        "UPDATE backtest_result_verdicts SET status='failed', detail='stale'",
    )

    result = repo.backtest_result(ANCHOR_ID)

    assert result.run_id == ANCHOR_ID
    assert result.events
    assert _verdict(path, ANCHOR_ID) == (
        *_current_key(path, ANCHOR_ID),
        "verified",
        None,
    )


def test_concurrent_opens_of_a_stale_verdict_leave_one_current_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both readers observe the same stale row before either writes; the
    late writer then tries a conflicting outcome, which only the CAS stops."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _execute(path, "UPDATE backtest_result_verdicts SET verifier_version='old.v0'")
    observed = threading.Barrier(2)
    first_written = threading.Event()
    errors: list[BaseException] = []
    original = BacktestRepository._write_result_verdict

    def interleaved(self, run_id, observed_row, key, status, detail):
        observed.wait(3)
        if threading.current_thread().name == "late":
            assert first_written.wait(3)
            status, detail = "failed", "late stale writer"
        original(self, run_id, observed_row, key, status, detail)
        first_written.set()

    monkeypatch.setattr(BacktestRepository, "_write_result_verdict", interleaved)

    def open_result() -> None:
        try:
            _repo(path).backtest_result(ANCHOR_ID)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=open_result, name=name) for name in ("early", "late")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with sqlite3.connect(path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM backtest_result_verdicts").fetchone()
    assert errors == []
    assert count == (1,)
    assert _verdict(path, ANCHOR_ID) == (
        *_current_key(path, ANCHOR_ID),
        "verified",
        None,
    )


def test_backfill_records_failures_and_a_rerun_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    _execute(path, "DELETE FROM backtest_result_verdicts")
    _tamper_trade_log(path, OTHER_ID)

    assert repo.backfill_backtest_result_verdicts() == 2

    anchor = _verdict(path, ANCHOR_ID)
    other = _verdict(path, OTHER_ID)
    assert anchor is not None and anchor[4] == "verified"
    assert other is not None and other[4] == "failed"
    calls = _count_backtest_result_calls(monkeypatch)
    assert repo.backfill_backtest_result_verdicts() == 0
    assert calls == []


def test_verdict_write_failure_never_changes_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    _execute(path, "UPDATE backtest_result_verdicts SET verifier_version='old.v0'")
    _tamper_trade_log(path, OTHER_ID)

    def failing_write(*_args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "_write_result_verdict", failing_write)

    assert repo.backtest_result(ANCHOR_ID).run_id == ANCHOR_ID
    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(OTHER_ID)
    verdict = _verdict(path, ANCHOR_ID)
    assert verdict is not None and verdict[2] == "old.v0"


def test_missing_run_writes_no_verdict(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)

    with pytest.raises(StrategyJobNotFound):
        repo.backtest_result("missing-run")
    assert _verdict(path, "missing-run") is None


def test_lists_page_ten_rows_without_rebuilding_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    for index in range(12):
        _complete_run(path, repo, run_id=f"run-{index:02d}", enqueue_seq=index + 1)
    calls = _count_backtest_result_calls(monkeypatch)

    first = repo.list_backtest_activities()
    second = repo.list_backtest_activities(limit=10, offset=10)
    summary = StrategyOutcomeService(repo).build_summary()

    assert calls == []
    assert len(first) == 10
    assert [item.job.id for item in second] == ["run-01", "run-00"]
    assert summary.verified_count == 12
    assert summary.runs


def test_strategies_screen_reads_summaries_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AD-9 (gh-131): the Strategies landing and All runs contexts never
    rebuild a full Result per row."""
    from types import SimpleNamespace

    from app.api.routes import strategy_manager as routes

    path = tmp_path / "backtest.db"
    repo = _repo(path)
    for index in range(12):
        _complete_run(path, repo, run_id=f"run-{index:02d}", enqueue_seq=index + 1)
    calls = _count_backtest_result_calls(monkeypatch)

    landing = routes._strategy_manager_context(
        repo,
        prices=None,  # type: ignore[arg-type]
        experiments=SimpleNamespace(  # type: ignore[arg-type]
            pending_details=lambda *, limit: ()
        ),
        setup_required=False,
        ready_count=5,
        coverage_ready=True,
        setup_notice=None,
        activated_at=None,
    )
    runs = routes._backtest_activities_context(repo, 2)

    assert calls == []
    assert landing["outcome_summary"] is not None
    assert landing["backtest_failed_count"] == 0
    assert "backtest_activities" not in landing
    assert len(runs["backtest_activities"]) == 2  # type: ignore[arg-type]


def test_summaries_carry_display_evidence_and_compare(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)

    page = repo.recent_backtest_result_summaries()
    left, right = repo.with_display_evidence(page.results)
    full = repo.backtest_result(ANCHOR_ID)
    anchor = left if left.run_id == ANCHOR_ID else right

    assert anchor.equity_curve == full.equity_curve
    assert anchor.metric_availability == full.metric_availability
    assert anchor.metrics == full.metrics
    assert anchor.equity_point_count == len(full.equity_curve)
    assert anchor.closed_trade_count == 1
    assert repo.compare_result_summaries(left, right).eligible is True


# ---------------------------------------------------------------------------
# gh-133 review patches
# ---------------------------------------------------------------------------

#: Every ``BacktestRepository`` method whose rules decide a Result's verdict.
_VERIFIER_METHODS = (
    "backtest_result",
    "_reconstruct_result",
    "_load_strategy_run_row",
    "_load_entry_selection",
    "_canonical_result_payload",
    "_parse_trade_log_event",
    "_parse_equity_curve_rows",
    "_parse_equity_curve_point",
)


def _verifier_source_digest() -> str:
    from app.services.backtest import metrics

    sources = [
        inspect.getsource(getattr(BacktestRepository, name))
        for name in _VERIFIER_METHODS
    ]
    sources.append(inspect.getsource(metrics))
    return hashlib.sha256("\n".join(sources).encode("utf-8")).hexdigest()


def test_result_verifier_version_tracks_the_verification_source() -> None:
    assert _verifier_source_digest() == RESULT_VERIFIER_SOURCE_DIGEST, (
        "Backtest Result verification source changed: bump "
        "RESULT_VERIFIER_VERSION in app/repositories/backtest_repo.py if the "
        "verification rules changed, then set RESULT_VERIFIER_SOURCE_DIGEST to "
        f"{_verifier_source_digest()!r}."
    )


def _seed_two_runs(path: Path) -> BacktestRepository:
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2)
    return repo


def test_backfill_survives_unexpected_errors_and_counts_current_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _seed_two_runs(path)
    _execute(path, "DELETE FROM backtest_result_verdicts")
    original = BacktestRepository.backtest_result

    def flaky(self: BacktestRepository, run_id: str, **kwargs: bool):
        if run_id == OTHER_ID:
            raise RuntimeError("unexpected")
        return original(self, run_id, **kwargs)

    monkeypatch.setattr(BacktestRepository, "backtest_result", flaky)

    assert repo.backfill_backtest_result_verdicts() == 1
    assert _verdict(path, OTHER_ID) is None


def test_backfill_records_a_damaged_run_identity_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _seed_two_runs(path)
    _execute(
        path,
        "DELETE FROM backtest_result_verdicts",
        "DROP TRIGGER IF EXISTS strategy_run_immutable_update",
        f"UPDATE strategy_runs SET starting_capital='bad' WHERE id='{OTHER_ID}'",
    )

    assert repo.backfill_backtest_result_verdicts() == 2

    other = _verdict(path, OTHER_ID)
    assert other is not None and other[4:] == (
        "failed",
        "stored strategy run is invalid",
    )
    calls = _count_backtest_result_calls(monkeypatch)
    assert repo.backfill_backtest_result_verdicts() == 0
    assert calls == []


def test_list_flags_invalid_metrics_under_a_matching_verdict(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    bad = '{"total_return": "oops"}'
    _execute(
        path,
        "DROP TRIGGER IF EXISTS backtest_result_evidence_immutable",
        f"UPDATE backtest_results SET metrics_json='{bad}', "
        "note_version=note_version+1",
        "UPDATE backtest_result_verdicts SET metrics_digest="
        f"'{hashlib.sha256(bad.encode()).hexdigest()}'",
    )

    (activity,) = repo.list_backtest_activities()

    assert activity.result_verdict == "failed"
    assert activity.metrics is None
    assert activity.result_error == "stored backtest result is invalid"


def test_outcome_panel_drops_a_run_with_damaged_display_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    # A second Strategy makes both runs one cohort, so both are display rows.
    _complete_run(path, repo, run_id=OTHER_ID, enqueue_seq=2, strategy_id="other_v1")
    _execute(
        path,
        "DROP TRIGGER IF EXISTS equity_curve_immutable_update",
        f"UPDATE equity_curve SET cash_base='garbage' WHERE run_id='{OTHER_ID}'",
    )

    page = repo.recent_backtest_result_summaries()
    (kept,) = repo.with_display_evidence(page.results)
    summary = StrategyOutcomeService(repo).build_summary()

    assert kept.run_id == ANCHOR_ID
    assert summary.state != "unavailable"
    assert [run.run_id for run in summary.runs] == [ANCHOR_ID]
    assert summary.integrity_excluded_count == 1


def test_completion_records_failed_when_metric_availability_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.backtest import metrics

    def broken(**_kwargs: object) -> None:
        raise metrics.MetricsError("empty_equity_curve", "equity curve is empty")

    monkeypatch.setattr(metrics, "metric_availability", broken)
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)

    verdict = _verdict(path, ANCHOR_ID)
    assert verdict is not None
    assert verdict[:4] == _current_key(path, ANCHOR_ID)
    assert verdict[4:] == ("failed", "equity curve is empty")


def test_unchanged_outcome_with_a_different_detail_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _tamper_trade_log(path, ANCHOR_ID)
    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(ANCHOR_ID)
    _execute(path, "UPDATE backtest_result_verdicts SET detail='older wording'")
    writes: list[object] = []
    monkeypatch.setattr(repo, "_write_result_verdict", lambda *a: writes.append(a))

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(ANCHOR_ID)

    assert writes == []


def test_verdict_cas_rejects_a_changed_metrics_digest(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    observed = _verdict(path, ANCHOR_ID)
    assert observed is not None
    stale = (*observed[:3], "0" * 64, *observed[4:])
    digest, schema, version, metrics_digest = map(str, _current_key(path, ANCHOR_ID))
    key = (digest, schema, version, metrics_digest)

    repo._write_result_verdict(ANCHOR_ID, stale, key, "failed", "stale writer")

    assert _verdict(path, ANCHOR_ID) == observed


def test_recent_summaries_exclude_a_contract_without_its_audit_manifest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _complete_run(path, repo, run_id=ANCHOR_ID, enqueue_seq=1)
    _execute(
        path,
        "DROP TRIGGER IF EXISTS backtest_result_evidence_immutable",
        "UPDATE backtest_results SET "
        "audit_contract_version='candidate_allocation_audit.v1', "
        "note_version=note_version+1",
    )

    page = repo.recent_backtest_result_summaries()

    assert page.results == ()
    assert page.integrity_excluded_count == 1


def test_recent_summaries_cap_skips_unverified_rows(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    for index in range(3):
        _complete_run(path, repo, run_id=f"run-{index}", enqueue_seq=index + 1)
    newest = repo.recent_backtest_result_summaries(limit=2).results[0].run_id
    _execute(path, f"DELETE FROM backtest_result_verdicts WHERE run_id='{newest}'")

    page = repo.recent_backtest_result_summaries(limit=2)

    assert len(page.results) == 2
    assert newest not in {item.run_id for item in page.results}
    assert page.unverified_count == 1
    assert page.inspected_count == 2


def test_list_availability_does_not_invent_a_sharpe_reason() -> None:
    from app.repositories.backtest_repo import _summary_availability
    from app.services.backtest.metrics import (
        BacktestMetricsV1,
        MetricUnavailableReason,
    )

    availability = _summary_availability(
        BacktestMetricsV1(
            total_return=0.1, sharpe_ratio=None, win_rate=None, max_drawdown=0.0
        )
    )

    assert availability.win_rate_unavailable is MetricUnavailableReason.NO_CLOSED_TRADES
    assert availability.sharpe_unavailable is None


def test_failed_result_count_spans_every_page(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    for index in range(12):
        _complete_run(path, repo, run_id=f"run-{index:02d}", enqueue_seq=index + 1)
    for run_id in ("run-00", "run-01"):
        _tamper_trade_log(path, run_id)
        with pytest.raises(BacktestIntegrityError):
            repo.backtest_result(run_id)

    first_page = repo.list_backtest_activities()

    assert not any(item.result_error for item in first_page)
    assert repo.failed_backtest_result_count() == 2
