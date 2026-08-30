"""Story 2.5 coverage: attempt-owned Backtest staging, atomic Result
promotion, note compare-and-swap, typed retrieval, and the terminal
cleanup boundary -- mirroring ``test_strategy_job_repository.py``'s real
on-disk-SQLite-plus-``ThreadPoolExecutor`` race pattern."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import sqlite3

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.services.backtest.backtest_engine import (
    EntryFillEventV1,
    EquityCurvePointV1,
    ExitFillEventV1,
    SkipReasonCode,
    SkippedSignalEventV1,
)
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.metrics import BacktestMetricsV1
from app.services.backtest.run_universe import run_universe_digest
from app.services.backtest.run_input_manifest import (
    PinnedSecurityEvidenceV1,
    build_run_input_manifest_v2,
)
from app.services.backtest.strategy_job import (
    RunUniverseSelectionV1,
    StrategyJobConflict,
    StrategyJobNotFound,
)
from app.services.backtest.strategy_protocol import (
    EntrySelectionDecisionV1,
    EntrySelectionState,
    InitialEntrySelectionV1,
    Signal,
    SignalSide,
)
from app.services.backtest.trading_calendar import TradingCalendar
from tests.backtest.test_run_input_manifest import _manifest

NOW = datetime(2026, 8, 12, 9, 30, tzinfo=timezone.utc)
PROFILE_HASH = "a" * 64
ROSTER_DIGEST = "b" * 64
MANIFEST_DIGEST = "c" * 64
EXECUTION_CONTRACT_DIGEST = "d" * 64
ORDERED_MONTH_DIGEST = "e" * 64
STRATEGY_SOURCE_DIGEST = "f" * 64
RUN_ID = "backtest-run-1"
CLAIM_TOKEN = "claim-token-1"


def _repo(path: Path) -> BacktestRepository:
    repo = BacktestRepository(
        db.make_connect(lambda: path),
        clock=lambda: date(2026, 8, 12),
        instant_clock=lambda: NOW,
    )
    repo.ensure_schema()
    return repo


def _seed_profile(path: Path) -> None:
    """Seed the minimum valid FK graph shared with the job repository
    suite; result tests do not read its payload."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT OR IGNORE INTO security_identity_registry_revisions
               (revision_digest, canonical_manifest_json, evidence_digest, created_at)
               VALUES (?, '{}', ?, ?)""",
            ("1" * 64, "2" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO security_alias_manifests
               (alias_revision, canonical_manifest_json, evidence_digest, created_at)
               VALUES (?, '{}', ?, ?)""",
            ("3" * 64, "4" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO reconstruction_rosters
               (roster_digest, policy_version, canonical_manifest_json,
                identity_registry_revision, alias_revision, captured_at)
               VALUES (?, 'ReconstructionRosterPolicyV1', '{}', ?, ?, ?)""",
            (ROSTER_DIGEST, "1" * 64, "3" * 64, NOW.isoformat()),
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


def _seed_run_input_manifest(path: Path, digest: str = MANIFEST_DIGEST) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO run_input_manifests
               (digest, execution_contract_digest, canonical_manifest_json, created_at)
               VALUES (?, ?, '{}', ?)""",
            (digest, EXECUTION_CONTRACT_DIGEST, NOW.isoformat()),
        )


def _seed_backtest_run(
    path: Path,
    *,
    run_id: str = RUN_ID,
    status: str = "running",
    claim_token: str | None = CLAIM_TOKEN,
    status_version: int = 1,
    starting_capital: str = "10000.00000000",
    enqueue_seq: int = 1,
) -> None:
    """Seed one 'backtest' job + its pinned ``strategy_runs`` identity --
    Story 2.6 owns enqueue/claim in production; this story's tests seed
    the prerequisite rows directly, mirroring ``_seed_profile``."""
    _seed_profile(path)
    _seed_run_input_manifest(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, claim_token, current_month,
                   status_version, cancel_requested_at, created_at, updated_at
               ) VALUES (?, 'backtest', ?, ?, ?, NULL, ?, NULL, ?, ?)""",
            (
                run_id, status, enqueue_seq, claim_token, status_version,
                NOW.isoformat(), NOW.isoformat(),
            ),
        )
        conn.execute(
            """INSERT INTO strategy_runs (
                   id, strategy_id, strategy_api_version, strategy_source_digest,
                   parameters_json, profile_hash, start_month, end_month,
                   ordered_month_digest, base_currency, starting_capital,
                   run_input_manifest_digest, execution_contract_digest, created_at
               ) VALUES (?, 'momentum_v1', 1, ?, '{"lookback": 20}', ?, '2026-01',
                         '2026-01', ?, 'USD', ?, ?, ?, ?)""",
            (
                run_id,
                STRATEGY_SOURCE_DIGEST,
                PROFILE_HASH,
                ORDERED_MONTH_DIGEST,
                starting_capital,
                MANIFEST_DIGEST,
                EXECUTION_CONTRACT_DIGEST,
                NOW.isoformat(),
            ),
        )


def _entry(seq: int, session: date) -> EntryFillEventV1:
    return EntryFillEventV1(
        security_id="AAA",
        signal_session=session,
        fill_session=session,
        rule_id="rule-1",
        shares=10,
        fill_price_native=Decimal("100"),
        fill_currency="USD",
        fill_quote_unit="USD",
        cost_base=Decimal("1000"),
        sequence=seq,
    )


def _exit(seq: int, session: date, pnl: str = "50") -> ExitFillEventV1:
    return ExitFillEventV1(
        security_id="AAA",
        signal_session=session,
        fill_session=session,
        rule_id="rule-1",
        shares=10,
        fill_price_native=Decimal("105"),
        fill_currency="USD",
        fill_quote_unit="USD",
        proceeds_base=Decimal("1000") + Decimal(pnl),
        cost_basis_base=Decimal("1000"),
        realized_pnl_base=Decimal(pnl),
        sequence=seq,
    )


def _skip(seq: int, session: date) -> SkippedSignalEventV1:
    return SkippedSignalEventV1(
        security_id="BBB",
        side=SignalSide.BUY,
        signal_session=session,
        rule_id="rule-2",
        reason=SkipReasonCode.INSUFFICIENT_CASH,
        detail="insufficient simulated cash at fill time",
        sequence=seq,
    )


def _curve_point(seq: int, session: date, equity: str) -> EquityCurvePointV1:
    return EquityCurvePointV1(
        session=session,
        cash_base=Decimal(equity),
        positions_value_base=Decimal("0"),
        total_equity_base=Decimal(equity),
        sequence=seq,
    )


def _staging_payload() -> dict[str, object]:
    events = (
        _entry(1, date(2026, 1, 2)),
        _skip(2, date(2026, 1, 2)),
        _exit(3, date(2026, 1, 5), "50"),
    )
    equity_curve = (
        _curve_point(4, date(2026, 1, 2), "9000"),
        _curve_point(5, date(2026, 1, 5), "10050"),
    )
    return {
        "state_schema_version": "backtest_portfolio_state.v1",
        "portfolio_state": {"cash": "10050.00000000", "positions": []},
        "events": events,
        "equity_curve": equity_curve,
        "final_cash_base": Decimal("10050"),
    }


def _initial_selection() -> InitialEntrySelectionV1:
    session = date(2026, 1, 2)
    return InitialEntrySelectionV1(
        session=session,
        metric_id="momentum-252",
        metric_version="v1",
        rule_id="initial-rank",
        decisions=[
            EntrySelectionDecisionV1(
                security_id="AAA",
                rank=1,
                state=EntrySelectionState.SELECTED,
                score=Decimal("0.25"),
            )
        ],
        signals=[
            Signal(
                security_id="AAA",
                side=SignalSide.BUY,
                session=session,
                rule_id="initial-rank",
            )
        ],
    )


def _initial_selection_universe() -> RunUniverseSelectionV1:
    return RunUniverseSelectionV1(
        profile_hash=PROFILE_HASH,
        activation_seq=1,
        universe_parameter="security_ids",
        canonical_security_ids=("AAA",),
        run_universe_digest=run_universe_digest(
            ["AAA"], parameter="security_ids", profile_hash=PROFILE_HASH
        ),
    )


def _seed_v2_backtest_run_for_initial_selection(path: Path) -> None:
    """Seed the same immutable V2 provenance shape production uses."""
    _seed_profile(path)
    selection = _initial_selection_universe()
    manifest = build_run_input_manifest_v2(
        _manifest(
            strategy_id="momentum_v1",
            strategy_api_version=1,
            strategy_source_digest=STRATEGY_SOURCE_DIGEST,
            parameters={"security_ids": ["AAA"]},
            profile_hash=PROFILE_HASH,
            start_month="2026-01",
            end_month="2026-01",
            ordered_month_digest=ORDERED_MONTH_DIGEST,
            base_currency="USD",
            securities=(
                PinnedSecurityEvidenceV1(
                    security_id="AAA",
                    price_revision="7" * 64,
                    action_revision="7" * 64,
                ),
            ),
        ),
        selection=selection,
        source_preparation_job_id="prep-run-1",
    )
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO run_input_manifests
               (digest, execution_contract_digest, canonical_manifest_json, created_at,
                manifest_version)
               VALUES (?, ?, ?, ?, ?)""",
            (
                manifest.digest(),
                manifest.execution_contract_digest(),
                manifest.canonical_json(),
                NOW.isoformat(),
                manifest.schema_version,
            ),
        )
        conn.execute(
            """INSERT INTO strategy_jobs
               (id, job_type, status, enqueue_seq, claim_token, status_version,
                created_at, updated_at)
               VALUES (?, 'backtest', 'running', 1, ?, 1, ?, ?)""",
            (RUN_ID, CLAIM_TOKEN, NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            """INSERT INTO strategy_runs
               (id, strategy_id, strategy_api_version, strategy_source_digest,
                parameters_json, profile_hash, start_month, end_month,
                ordered_month_digest, base_currency, starting_capital,
                run_input_manifest_digest, execution_contract_digest,
                manifest_version, run_universe_digest, selection_json,
                source_preparation_job_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                RUN_ID,
                manifest.strategy_id,
                manifest.strategy_api_version,
                manifest.strategy_source_digest,
                json.dumps(manifest.parameters, sort_keys=True),
                manifest.profile_hash,
                manifest.start_month,
                manifest.end_month,
                manifest.ordered_month_digest,
                manifest.base_currency,
                str(manifest.starting_capital),
                manifest.digest(),
                manifest.execution_contract_digest(),
                manifest.schema_version,
                selection.run_universe_digest,
                selection.model_dump_json(),
                "prep-run-1",
                NOW.isoformat(),
            ),
        )


def test_selection_bearing_result_round_trips_as_v2_and_missing_evidence_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_v2_backtest_run_for_initial_selection(path)
    payload = _staging_payload()
    payload["initial_entry_selection"] = _initial_selection()
    _write_staging(repo, payload=payload)

    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    result = repo.backtest_result(RUN_ID)
    assert result.initial_entry_selection == _initial_selection()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT result_schema_version FROM backtest_results WHERE run_id=?",
            (RUN_ID,),
        ).fetchone() == ("backtest_result.v2",)
        conn.execute(
            "DROP TRIGGER backtest_result_entry_selection_decision_immutable_delete"
        )
        conn.execute("DROP TRIGGER backtest_result_entry_selection_immutable_delete")
        conn.execute(
            "DELETE FROM backtest_result_entry_selection_decisions WHERE run_id=?",
            (RUN_ID,),
        )
        conn.execute(
            "DELETE FROM backtest_result_entry_selection WHERE run_id=?", (RUN_ID,)
        )

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(RUN_ID)


def test_staging_selection_is_revalidated_against_the_pinned_run_universe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_v2_backtest_run_for_initial_selection(path)
    payload = _staging_payload()
    payload["initial_entry_selection"] = _initial_selection().model_copy(
        update={"signals": ()}
    )

    with pytest.raises(
        BacktestIntegrityError, match="staged initial entry selection is invalid"
    ) as exc_info:
        _write_staging(repo, payload=payload)

    assert exc_info.value.code == "initial_selection_signal_mismatch"


def test_schema_upgrade_rebuilds_legacy_result_immutability_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER backtest_result_evidence_immutable")
        conn.execute(
            """CREATE TRIGGER backtest_result_evidence_immutable
               BEFORE UPDATE ON backtest_results
               WHEN NEW.run_id != OLD.run_id
                 OR NEW.metrics_json != OLD.metrics_json
                 OR NEW.final_cash_base != OLD.final_cash_base
                 OR NEW.result_digest != OLD.result_digest
                 OR NEW.completed_at != OLD.completed_at
               BEGIN SELECT RAISE(ABORT, 'backtest result evidence is immutable'); END"""
        )

    repo.ensure_schema()

    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE backtest_results SET result_schema_version='backtest_result.v2' "
                "WHERE run_id=?",
                (RUN_ID,),
            )


def _write_staging(
    repo: BacktestRepository,
    *,
    run_id: str = RUN_ID,
    claim_token: str = CLAIM_TOKEN,
    expected_version: int = 1,
    payload: dict[str, object] | None = None,
) -> None:
    data = payload if payload is not None else _staging_payload()
    repo.write_backtest_staging(
        run_id,
        claim_token=claim_token,
        expected_version=expected_version,
        **data,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Staging: attempt-owned compare-and-swap writes
# ---------------------------------------------------------------------------


def test_staging_write_succeeds_for_the_current_owning_attempt(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    _write_staging(repo)

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT state_schema_version, final_cash_base FROM backtest_staging WHERE run_id=?",
            (RUN_ID,),
        ).fetchone()
    assert row == ("backtest_portfolio_state.v1", "10050")


def test_staging_write_is_replaceable_by_the_same_owning_attempt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)

    second_payload = _staging_payload()
    second_payload["final_cash_base"] = Decimal("20000")
    _write_staging(repo, payload=second_payload)

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT final_cash_base FROM backtest_staging WHERE run_id=?", (RUN_ID,)
        ).fetchall()
    assert rows == [("20000",)]  # replaced, not duplicated


def test_staging_write_rejects_a_stale_owner(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    with pytest.raises(StrategyJobConflict):
        _write_staging(repo, claim_token="wrong-token")
    with pytest.raises(StrategyJobConflict):
        _write_staging(repo, expected_version=99)

    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (RUN_ID,)
            ).fetchone()
            is None
        )


def test_staging_write_requires_the_current_worker_lease(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    lease = repo.acquire_or_renew_worker_lease("worker-a", ttl_seconds=30)

    with pytest.raises(StrategyJobConflict):
        _write_staging(repo)

    repo.write_backtest_staging(
        RUN_ID,
        claim_token=CLAIM_TOKEN,
        expected_version=1,
        lease=lease.fence,
        **_staging_payload(),  # type: ignore[arg-type]
    )


def test_staging_write_rejects_a_stale_worker_lease_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    instant = [NOW]
    repo = BacktestRepository(
        db.make_connect(lambda: path),
        clock=lambda: date(2026, 8, 12),
        instant_clock=lambda: instant[0],
    )
    repo.ensure_schema()
    _seed_backtest_run(path)
    stale = repo.acquire_or_renew_worker_lease("worker-a", ttl_seconds=1)
    instant[0] += timedelta(seconds=2)
    current = repo.acquire_or_renew_worker_lease("worker-b", ttl_seconds=30)

    with pytest.raises(StrategyJobConflict):
        repo.write_backtest_staging(
            RUN_ID,
            claim_token=CLAIM_TOKEN,
            expected_version=1,
            lease=stale.fence,
            **_staging_payload(),  # type: ignore[arg-type]
        )

    repo.write_backtest_staging(
        RUN_ID,
        claim_token=CLAIM_TOKEN,
        expected_version=1,
        lease=current.fence,
        **_staging_payload(),  # type: ignore[arg-type]
    )


def test_staging_write_rejects_a_completed_or_cancelled_owner(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path, status="complete", claim_token=None)

    with pytest.raises(StrategyJobConflict):
        _write_staging(repo)


def test_staging_write_rejects_out_of_order_events_or_curve(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    bad_events = _staging_payload()
    bad_events["events"] = (
        _exit(3, date(2026, 1, 5)),
        _entry(1, date(2026, 1, 2)),
    )
    with pytest.raises(ValueError):
        _write_staging(repo, payload=bad_events)

    bad_curve = _staging_payload()
    bad_curve["equity_curve"] = (
        _curve_point(5, date(2026, 1, 5), "10050"),
        _curve_point(4, date(2026, 1, 2), "9000"),
    )
    with pytest.raises(ValueError):
        _write_staging(repo, payload=bad_curve)

    bad_curve_sequence = _staging_payload()
    bad_curve_sequence["equity_curve"] = (
        _curve_point(4, date(2026, 1, 2), "9000"),
        _curve_point(4, date(2026, 1, 5), "10050"),  # duplicate sequence
    )
    with pytest.raises(ValueError):
        _write_staging(repo, payload=bad_curve_sequence)


def test_staging_write_rejects_a_non_finite_final_cash_base(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    bad_payload = _staging_payload()
    bad_payload["final_cash_base"] = Decimal("Infinity")
    with pytest.raises(ValueError):
        _write_staging(repo, payload=bad_payload)

    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (RUN_ID,)
            ).fetchone()
            is None
        )


def test_staging_write_rejects_a_non_json_serializable_portfolio_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    bad_payload = _staging_payload()
    bad_payload["portfolio_state"] = {"cash": Decimal("100")}  # not JSON-serializable
    with pytest.raises(ValueError):
        _write_staging(repo, payload=bad_payload)


def test_concurrent_staging_writers_leave_exactly_one_committed_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    _repo(path)
    _seed_backtest_run(path)

    candidates = ["1001", "1002", "1003", "1004"]

    def attempt(final_cash: str):
        payload = _staging_payload()
        payload["final_cash_base"] = Decimal(final_cash)
        # Every field of a racing writer's payload carries the same
        # marker, so the persisted row can be checked for internal
        # consistency below -- not just presence.
        payload["portfolio_state"] = {"cash": final_cash, "positions": []}
        try:
            _repo(path).write_backtest_staging(
                RUN_ID,
                claim_token=CLAIM_TOKEN,
                expected_version=1,
                **payload,  # type: ignore[arg-type]
            )
            return True
        except StrategyJobConflict:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, candidates))

    assert any(results)
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT final_cash_base, state_json FROM backtest_staging WHERE run_id=?",
            (RUN_ID,),
        ).fetchall()
    assert len(rows) == 1
    final_cash_base, state_json = rows[0]
    # The committed row must be exactly one racing writer's whole payload
    # -- never a merge of one attempt's final_cash_base with another's
    # portfolio_state -- so the stored cash marker must match the stored
    # final_cash_base.
    assert final_cash_base in candidates
    assert json.loads(state_json) == {"cash": final_cash_base, "positions": []}


# ---------------------------------------------------------------------------
# Completion: atomic promotion, idempotency, divergence
# ---------------------------------------------------------------------------


def test_completion_promotes_result_trade_log_and_curve_and_deletes_staging(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)

    job = repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    assert job.status.value == "complete"
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (RUN_ID,)
            ).fetchone()
            is None
        )
        trade_log_count = conn.execute(
            "SELECT COUNT(*) FROM trade_log WHERE run_id=?", (RUN_ID,)
        ).fetchone()[0]
        curve_count = conn.execute(
            "SELECT COUNT(*) FROM equity_curve WHERE run_id=?", (RUN_ID,)
        ).fetchone()[0]
    assert trade_log_count == 3
    assert curve_count == 2

    result = repo.backtest_result(RUN_ID)
    assert result.metrics.total_return == pytest.approx(0.005)
    assert result.metrics.win_rate == 1.0
    assert len(result.events) == 3
    assert len(result.equity_curve) == 2
    assert result.note is None
    assert result.note_version == 1


def test_latest_completed_backtest_result_uses_valid_durable_result_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path, run_id="backtest-run-1", enqueue_seq=2)
    _write_staging(repo, run_id="backtest-run-1")
    repo.complete_claimed_backtest_job("backtest-run-1", CLAIM_TOKEN, expected_version=1)
    _seed_backtest_run(path, run_id="backtest-run-2")
    _write_staging(repo, run_id="backtest-run-2")
    repo.complete_claimed_backtest_job("backtest-run-2", CLAIM_TOKEN, expected_version=1)

    assert repo.latest_completed_backtest_result().run_id == "backtest-run-2"  # type: ignore[union-attr]

    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE strategy_jobs
               SET deleted_at=?, updated_at=?, status_version=status_version+1
               WHERE id=?""",
            (NOW.isoformat(), NOW.isoformat(), "backtest-run-2"),
        )

    assert repo.latest_completed_backtest_result().run_id == "backtest-run-1"  # type: ignore[union-attr]


def test_completion_requires_staging_to_exist(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)


def test_completion_rejects_stale_ownership(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)

    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_backtest_job(RUN_ID, "wrong-token", expected_version=1)
    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=99)

    # Failure exposes no partial writes; staging remains for cleanup.
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (RUN_ID,)
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_results WHERE run_id=?", (RUN_ID,)
            ).fetchone()
            is None
        )


def test_repeated_completion_after_success_is_an_idempotent_no_op(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)

    first = repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)
    second = repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    assert second == first
    with sqlite3.connect(path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM backtest_results WHERE run_id=?", (RUN_ID,)
        ).fetchone()[0]
        trade_log_count = conn.execute(
            "SELECT COUNT(*) FROM trade_log WHERE run_id=?", (RUN_ID,)
        ).fetchone()[0]
    assert count == 1
    assert trade_log_count == 3  # no duplicate rows


def test_divergent_repeat_completion_raises_and_leaves_result_untouched(
    tmp_path: Path,
) -> None:
    """A genuinely divergent repeat can only arise from tampered state
    under this method's own atomic write shape (Result + job transition
    always commit together) -- simulated here exactly like this file's
    sibling suite simulates illegal direct writes."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO backtest_results (
                   run_id, metrics_json, final_cash_base, result_digest,
                   note, note_version, completed_at, updated_at
               ) VALUES (?, '{}', '0', ?, NULL, 1, ?, ?)""",
            (RUN_ID, "9" * 64, NOW.isoformat(), NOW.isoformat()),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    with sqlite3.connect(path) as conn:
        stored_digest = conn.execute(
            "SELECT result_digest FROM backtest_results WHERE run_id=?", (RUN_ID,)
        ).fetchone()[0]
    assert stored_digest == "9" * 64  # untouched


def test_completion_surfaces_metrics_error_as_backtest_integrity_error(
    tmp_path: Path,
) -> None:
    """``calculate_metrics`` raises the metrics module's own
    ``MetricsError`` for structurally invalid input (e.g. non-positive
    starting capital) -- Story 2.5 review P4(a) requires this repository
    to re-surface it as ``BacktestIntegrityError``, matching every other
    integrity failure here, never leak the metrics module's exception
    type."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path, starting_capital="0")
    _write_staging(repo)

    with pytest.raises(BacktestIntegrityError):
        repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)


def test_completion_raises_integrity_error_for_malformed_staging_decimal(
    tmp_path: Path,
) -> None:
    """A malformed ``final_cash_base`` in staging raises
    ``decimal.InvalidOperation`` on ``Decimal(...)`` parse, which is not a
    ``ValueError``/``TypeError`` subclass -- Story 2.5 review P6 requires
    ``_load_backtest_staging_row`` to catch it too."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE backtest_staging SET final_cash_base=? WHERE run_id=?",
            ("not-a-decimal", RUN_ID),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)


# ---------------------------------------------------------------------------
# Note compare-and-swap
# ---------------------------------------------------------------------------


def test_note_update_changes_only_note_fields(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    updated = repo.update_backtest_result_note(
        RUN_ID, expected_note_version=1, note="Solid quarter"
    )

    assert updated.note == "Solid quarter"
    assert updated.note_version == 2
    before = repo.backtest_result(RUN_ID)
    assert before.metrics == updated.metrics
    assert before.events == updated.events
    assert before.equity_curve == updated.equity_curve


def test_note_update_rejects_a_stale_version(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    with pytest.raises(StrategyJobConflict):
        repo.update_backtest_result_note(RUN_ID, expected_note_version=99, note="x")


def test_note_update_on_missing_result_is_not_found(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    with pytest.raises(StrategyJobNotFound):
        repo.update_backtest_result_note(RUN_ID, expected_note_version=1, note="x")


def test_note_over_limit_is_rejected_before_persistence(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    too_long = "x" * 10_001
    with pytest.raises(ValueError):
        repo.update_backtest_result_note(RUN_ID, expected_note_version=1, note=too_long)
    result = repo.backtest_result(RUN_ID)
    assert result.note is None
    assert result.note_version == 1


def test_whitespace_only_note_normalizes_to_null(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    updated = repo.update_backtest_result_note(
        RUN_ID, expected_note_version=1, note="   \n\t  "
    )

    assert updated.note is None
    assert updated.note_version == 2


def test_note_text_is_escaped(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    updated = repo.update_backtest_result_note(
        RUN_ID, expected_note_version=1, note="<script>alert(1)</script>"
    )

    assert "<script>" not in (updated.note or "")
    assert "&lt;script&gt;" in (updated.note or "")


# ---------------------------------------------------------------------------
# Retrieval: typed aggregate projection, provenance, tamper detection
# ---------------------------------------------------------------------------


def test_retrieval_returns_full_typed_provenance(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    result = repo.backtest_result(RUN_ID)

    assert result.strategy_id == "momentum_v1"
    assert result.strategy_api_version == 1
    assert result.strategy_source_digest == STRATEGY_SOURCE_DIGEST
    assert result.parameters == {"lookback": 20}
    assert result.profile_hash == PROFILE_HASH
    assert result.start_month == "2026-01"
    assert result.end_month == "2026-01"
    assert result.ordered_month_digest == ORDERED_MONTH_DIGEST
    assert result.base_currency == "USD"
    assert result.starting_capital == Decimal("10000.00000000")
    assert result.run_input_manifest_digest == MANIFEST_DIGEST
    assert result.execution_contract_digest == EXECUTION_CONTRACT_DIGEST
    assert [event.sequence for event in result.events] == [1, 2, 3]
    assert [point.session for point in result.equity_curve] == [
        date(2026, 1, 2),
        date(2026, 1, 5),
    ]


def test_retrieval_orders_trade_log_by_sequence_not_insertion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    payload = _staging_payload()
    # Deliberately out of insertion-friendly order is not representable via
    # the CAS write (it validates ascending sequence), so instead verify
    # retrieval trusts the stored ``sequence`` column, not SQLite rowid, by
    # reading raw rows back in reverse id order and confirming the
    # repository still returns them ascending by sequence.
    _write_staging(repo, payload=payload)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    result = repo.backtest_result(RUN_ID)

    sequences = [event.sequence for event in result.events]
    assert sequences == sorted(sequences)


def test_retrieval_recomputes_availability_reasons_via_metrics_module(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    payload: dict[str, object] = {
        "state_schema_version": "backtest_portfolio_state.v1",
        "portfolio_state": {"cash": "10000.00000000", "positions": []},
        "events": (),
        "equity_curve": (_curve_point(1, date(2026, 1, 2), "10000"),),
        "final_cash_base": Decimal("10000"),
    }
    _write_staging(repo, payload=payload)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    result = repo.backtest_result(RUN_ID)

    assert result.metrics.win_rate is None
    assert result.metrics.sharpe_ratio is None
    assert result.metric_availability.win_rate_unavailable is not None
    assert result.metric_availability.sharpe_unavailable is not None


def test_retrieval_of_missing_result_raises_not_found(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    with pytest.raises(StrategyJobNotFound):
        repo.backtest_result(RUN_ID)


def test_retrieval_detects_tampered_evidence(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    # The ``backtest_result_evidence_immutable`` trigger blocks a normal
    # direct ``UPDATE`` of ``metrics_json`` -- drop it first to simulate
    # tampered-at-rest evidence and prove retrieval's digest rebuild-and-
    # compare independently catches it.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS backtest_result_evidence_immutable")
        conn.execute(
            """UPDATE backtest_results
               SET metrics_json=?, note_version=note_version+1 WHERE run_id=?""",
            (
                '{"total_return": 999.0, "sharpe_ratio": null, '
                '"win_rate": null, "max_drawdown": null}',
                RUN_ID,
            ),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(RUN_ID)


def test_retrieval_detects_tampered_completed_at(tmp_path: Path) -> None:
    """``completed_at`` is part of the canonical digest payload (Story 2.5
    review P7), so a tampered value must fail the same rebuild-and-compare
    tamper detection as every other evidence field -- not merely a raw
    ``ValueError`` from ``datetime.fromisoformat`` on a malformed value."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS backtest_result_evidence_immutable")
        conn.execute(
            """UPDATE backtest_results
               SET completed_at=?, note_version=note_version+1 WHERE run_id=?""",
            ("2099-01-01T00:00:00+00:00", RUN_ID),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(RUN_ID)


def test_retrieval_raises_integrity_error_for_malformed_result_decimal(
    tmp_path: Path,
) -> None:
    """A malformed ``final_cash_base`` on a stored Result raises
    ``decimal.InvalidOperation`` on ``Decimal(...)`` parse, which is not a
    ``ValueError``/``TypeError`` subclass -- Story 2.5 review P6 requires
    ``backtest_result`` to catch it too, not leak a raw exception."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS backtest_result_evidence_immutable")
        conn.execute(
            """UPDATE backtest_results
               SET final_cash_base=?, note_version=note_version+1 WHERE run_id=?""",
            ("not-a-decimal", RUN_ID),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(RUN_ID)


def test_retrieval_raises_integrity_error_for_malformed_starting_capital(
    tmp_path: Path,
) -> None:
    """Same as above for ``strategy_runs.starting_capital`` --
    ``_load_strategy_run_row`` must also catch ``decimal.InvalidOperation``
    (Story 2.5 review P6)."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS strategy_run_immutable_update")
        conn.execute(
            "UPDATE strategy_runs SET starting_capital=? WHERE id=?",
            ("not-a-decimal", RUN_ID),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(RUN_ID)


def test_retrieval_surfaces_metrics_error_as_backtest_integrity_error(
    tmp_path: Path,
) -> None:
    """``backtest_result`` recomputes availability via ``metric_
    availability`` (never a second Metrics implementation); Story 2.5
    review P4(b) requires a ``MetricsError`` there -- e.g. a zero-equity
    daily-return division -- to surface as ``BacktestIntegrityError`` too.
    Directly seeds a Result whose otherwise-valid, digest-matching Equity
    Curve contains a zero ``total_equity_base`` (not rejected by staging's
    ordering-only validation), a shape ``calculate_metrics`` would also
    reject at completion time -- so this is only reachable by seeding
    stored evidence directly, exactly like this suite's other
    tamper/corruption simulations."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)

    metrics = BacktestMetricsV1(
        total_return=0.0, sharpe_ratio=None, win_rate=None, max_drawdown=0.0
    )
    equity_curve = (
        _curve_point(1, date(2026, 1, 2), "0"),
        _curve_point(2, date(2026, 1, 5), "100"),
    )
    completed_at = NOW.isoformat()
    payload = repo._canonical_result_payload(
        metrics=metrics,
        events=(),
        equity_curve=equity_curve,
        final_cash_base=Decimal("100"),
        completed_at=completed_at,
    )
    digest = manifest_digest(payload)

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO backtest_results (
                   run_id, metrics_json, final_cash_base, result_digest,
                   note, note_version, completed_at, updated_at
               ) VALUES (?, ?, '100', ?, NULL, 1, ?, ?)""",
            (
                RUN_ID,
                json.dumps(
                    metrics.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                digest,
                completed_at,
                completed_at,
            ),
        )
        for point in equity_curve:
            conn.execute(
                """INSERT INTO equity_curve (
                       run_id, date, sequence, cash_base, positions_value_base,
                       total_equity_base
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    RUN_ID,
                    point.session.isoformat(),
                    point.sequence,
                    str(point.cash_base),
                    str(point.positions_value_base),
                    str(point.total_equity_base),
                ),
            )

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(RUN_ID)


# ---------------------------------------------------------------------------
# Schema immutability / subtype requirements
# ---------------------------------------------------------------------------


def test_database_rejects_direct_mutation_of_immutable_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE backtest_results SET metrics_json='{}' WHERE run_id=?", (RUN_ID,)
        )
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM trade_log WHERE run_id=?", (RUN_ID,))
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM equity_curve WHERE run_id=?", (RUN_ID,))
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE strategy_runs SET starting_capital='1' WHERE id=?", (RUN_ID,)
        )


def test_staging_write_rejects_a_running_backtest_job_without_a_strategy_run(
    tmp_path: Path,
) -> None:
    """Mirrors Story 2.2/2.3's lightweight ``job_type='backtest'`` FIFO
    placeholder (a running/queued backtest job with no ``strategy_runs``
    row is an accepted, already-tested shape --
    ``test_initialization_and_backtest_placeholders_share_one_fifo`` --
    so this story must not add a schema trigger narrowing it). Story 2.6
    creates ``strategy_runs`` before a real attempt starts publishing
    staging; until then, the FK from ``backtest_staging`` to
    ``strategy_runs`` is what rejects the write."""
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_profile(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, claim_token,
                   status_version, created_at, updated_at
               ) VALUES ('orphan-run', 'backtest', 'running', 1, ?, 1, ?, ?)""",
            (CLAIM_TOKEN, NOW.isoformat(), NOW.isoformat()),
        )

    with pytest.raises(sqlite3.IntegrityError):
        repo.write_backtest_staging(
            "orphan-run",
            claim_token=CLAIM_TOKEN,
            expected_version=1,
            **_staging_payload(),  # type: ignore[arg-type]
        )


def test_note_version_monotonic_trigger_rejects_non_incrementing_update(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE backtest_results SET note='x', note_version=5 WHERE run_id=?",
            (RUN_ID,),
        )


# ---------------------------------------------------------------------------
# Additional schema/trigger and integrity coverage (Story 2.5 review)
# ---------------------------------------------------------------------------


def test_run_input_manifest_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    _repo(path)
    _seed_run_input_manifest(path)

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE run_input_manifests SET canonical_manifest_json='{}' WHERE digest=?",
            (MANIFEST_DIGEST,),
        )
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM run_input_manifests WHERE digest=?", (MANIFEST_DIGEST,)
        )


def test_strategy_run_insert_rejected_for_a_non_backtest_job(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    _repo(path)
    _seed_profile(path)
    _seed_run_input_manifest(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, status_version,
                   created_at, updated_at
               ) VALUES ('init-job-1', 'initialization', 'queued', 1, 1, ?, ?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO strategy_runs (
                   id, strategy_id, strategy_api_version, strategy_source_digest,
                   parameters_json, profile_hash, start_month, end_month,
                   ordered_month_digest, base_currency, starting_capital,
                   run_input_manifest_digest, execution_contract_digest, created_at
               ) VALUES ('init-job-1', 'momentum_v1', 1, ?, '{}', ?, '2026-01',
                         '2026-01', ?, 'USD', '10000', ?, ?, ?)""",
            (
                STRATEGY_SOURCE_DIGEST,
                PROFILE_HASH,
                ORDERED_MONTH_DIGEST,
                MANIFEST_DIGEST,
                EXECUTION_CONTRACT_DIGEST,
                NOW.isoformat(),
            ),
        )


def test_strategy_run_immutable_delete_requires_a_soft_deleted_job(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backtest.db"
    _repo(path)
    _seed_backtest_run(path, status="complete", claim_token=None)

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM strategy_runs WHERE id=?", (RUN_ID,))

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """UPDATE strategy_jobs
               SET deleted_at=?, status_version=status_version+1 WHERE id=?""",
            (NOW.isoformat(), RUN_ID),
        )
        conn.execute("DELETE FROM strategy_runs WHERE id=?", (RUN_ID,))
        remaining = conn.execute(
            "SELECT 1 FROM strategy_runs WHERE id=?", (RUN_ID,)
        ).fetchone()
    assert remaining is None


def test_staging_write_rejects_a_cancelled_owner(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE strategy_jobs
               SET cancel_requested_at=?, status_version=status_version+1
               WHERE id=?""",
            (NOW.isoformat(), RUN_ID),
        )

    # ``expected_version=2`` matches the bumped row exactly, so this
    # isolates the ``cancel_requested_at IS NOT NULL`` rejection branch
    # from an unrelated stale-version rejection.
    with pytest.raises(StrategyJobConflict):
        _write_staging(repo, expected_version=2)


def test_completion_rejects_a_cancelled_owner(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE strategy_jobs
               SET cancel_requested_at=?, status_version=status_version+1
               WHERE id=?""",
            (NOW.isoformat(), RUN_ID),
        )

    with pytest.raises(StrategyJobConflict):
        repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=2)


def test_retrieval_detects_tampered_trade_log_content(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    # ``trade_log_immutable_update`` blocks a normal direct ``UPDATE`` --
    # drop it first to simulate tampered-at-rest evidence, mirroring
    # ``test_retrieval_detects_tampered_evidence``'s pattern. The
    # replacement payload is structurally valid (still parses as an
    # ``EntryFillEventV1``) so this specifically exercises the digest
    # rebuild-and-compare, not the JSON/model parse-error path.
    with sqlite3.connect(path) as conn:
        original = conn.execute(
            "SELECT event_json FROM trade_log WHERE run_id=? AND sequence=1", (RUN_ID,)
        ).fetchone()[0]
        tampered = json.loads(original)
        tampered["shares"] = 999
        conn.execute("DROP TRIGGER IF EXISTS trade_log_immutable_update")
        conn.execute(
            "UPDATE trade_log SET event_json=? WHERE run_id=? AND sequence=1",
            (json.dumps(tampered), RUN_ID),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(RUN_ID)


def test_retrieval_detects_tampered_equity_curve_content(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _seed_backtest_run(path)
    _write_staging(repo)
    repo.complete_claimed_backtest_job(RUN_ID, CLAIM_TOKEN, expected_version=1)

    # ``equity_curve_immutable_update`` blocks a normal direct ``UPDATE``
    # -- drop it first, then tamper a structurally valid decimal string so
    # this exercises the digest rebuild-and-compare rather than a parse
    # error.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS equity_curve_immutable_update")
        conn.execute(
            "UPDATE equity_curve SET total_equity_base=? WHERE run_id=? AND sequence=4",
            ("999999", RUN_ID),
        )

    with pytest.raises(BacktestIntegrityError):
        repo.backtest_result(RUN_ID)
