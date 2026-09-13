"""Offline GH-619 benchmark for legacy versus append-batch staging.

Both runs use the same pinned completed source run and immutable historical
price database. Only a temporary clone of the backtest database is mutated.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import subprocess
import tempfile
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.core import config
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.backtest_engine import (
    EquityCurvePointV1,
    EntryFillEventV1,
    ExitFillEventV1,
    OpenPositionMarkEventV1,
    SplitAppliedEventV1,
    TradeLogEvent,
)
from app.services.backtest.strategy_job import StrategyJobStatus, WorkerLeaseFenceV1
from app.services.backtest.strategy_protocol import InitialEntrySelectionV1
from app.services.backtest.run_input_manifest import (
    ENGINE_VERSION,
    PROTOCOL_SCHEMA_VERSION,
    current_execution_contract_digest,
    current_execution_contract_payload,
    read_run_input_manifest,
)
import app.services.backtest.worker as worker_module
from app.services.backtest.worker import (
    BacktestExecutionEngine,
    _ClaimState,
    _PORTFOLIO_STATE_SCHEMA_VERSION,
)


REFERENCE_SECONDS = 3783.766
MODE_ORDER = (
    "Buy and Hold",
    "Darvas",
    "Moving Average",
    "Turtle Trend",
    "Minervini",
    "Weinstein",
    "Minervini, upgrade enabled",
    "Weinstein, upgrade enabled",
)
STRATEGY_IDS = {
    "Buy and Hold": "rtly-backtest-buy-and-hold",
    "Darvas": "rtly-backtest-darvas-box",
    "Moving Average": "rtly-backtest-moving-average",
    "Turtle Trend": "rtly-backtest-turtle-trend",
    "Minervini": "rtly-backtest-minervini",
    "Weinstein": "rtly-backtest-weinstein",
    "Minervini, upgrade enabled": "rtly-backtest-minervini",
    "Weinstein, upgrade enabled": "rtly-backtest-weinstein",
}


class _MeasuredRepository(BacktestRepository):
    """Repository counters for one disposable benchmark database."""

    def __init__(self, connect: Any, *, staging_mode: str) -> None:
        super().__init__(connect)
        self.staging_mode = staging_mode
        self.append_calls = 0
        self.compressed_bytes = 0
        self.uncompressed_bytes = 0
        self.promotion_seconds = 0.0
        self.last_error: str | None = None

    def append_backtest_staging_batch(self, run_id: str, **kwargs: Any) -> None:
        try:
            super().append_backtest_staging_batch(run_id, **kwargs)
        except Exception as exc:
            self.last_error = f"{exc!r}; cause={exc.__cause__!r}"
            raise
        self.append_calls += 1
        with self._connect() as conn:
            row = conn.execute(
                """SELECT length(payload_blob), uncompressed_bytes
                   FROM backtest_staging_batches
                   WHERE run_id=? AND batch_sequence=?""",
                (run_id, kwargs["batch_sequence"]),
            ).fetchone()
        if row is None:
            raise RuntimeError("append benchmark row disappeared")
        with self._connect() as conn:
            checkpoint = conn.execute(
                "SELECT length(state_json) FROM backtest_staging WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if checkpoint is None:
            raise RuntimeError("append benchmark checkpoint disappeared")
        self.compressed_bytes += int(row[0])
        self.uncompressed_bytes += int(checkpoint[0]) + int(row[1])

    def write_backtest_staging(self, run_id: str, **kwargs: Any) -> None:
        try:
            super().write_backtest_staging(run_id, **kwargs)
        except Exception as exc:
            self.last_error = f"{exc!r}; cause={exc.__cause__!r}"
            raise
        self.append_calls += 1
        with self._connect() as conn:
            row = conn.execute(
                """SELECT length(state_json), length(events_json),
                          length(equity_curve_json)
                   FROM backtest_staging WHERE run_id=?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("legacy staging row disappeared")
        self.uncompressed_bytes += sum(int(value) for value in row)

    def complete_claimed_backtest_job(self, *args: Any, **kwargs: Any) -> Any:
        started = perf_counter()
        try:
            return super().complete_claimed_backtest_job(*args, **kwargs)
        except Exception as exc:
            self.last_error = f"{exc!r}; cause={exc.__cause__!r}"
            raise
        finally:
            self.promotion_seconds += perf_counter() - started


@dataclass
class _LegacyStagingSink:
    """The pre-616 sink, retained here only as a benchmark control."""

    repository: BacktestRepository
    state: _ClaimState
    lease: Any = None
    events: list[TradeLogEvent] = None  # type: ignore[assignment]
    equity_curve: list[EquityCurvePointV1] = None  # type: ignore[assignment]
    open_positions: dict[str, Decimal] = None  # type: ignore[assignment]
    initial_entry_selection: InitialEntrySelectionV1 | None = None

    def __post_init__(self) -> None:
        self.events = []
        self.equity_curve = []
        self.open_positions = {}

    def publish_session(
        self,
        *,
        session: Any,
        events: tuple[TradeLogEvent, ...],
        equity_point: EquityCurvePointV1,
        initial_entry_selection: InitialEntrySelectionV1 | None = None,
    ) -> None:
        del session
        if initial_entry_selection is not None:
            if self.initial_entry_selection is not None:
                raise RuntimeError("initial selection was published twice")
            self.initial_entry_selection = initial_entry_selection
        self.events.extend(events)
        self.equity_curve.append(equity_point)
        for event in events:
            if isinstance(event, EntryFillEventV1):
                self.open_positions[event.security_id] = Decimal(event.shares)
            elif isinstance(event, ExitFillEventV1):
                self.open_positions.pop(event.security_id, None)
            elif isinstance(event, SplitAppliedEventV1):
                self.open_positions[event.security_id] = event.shares_after
            elif isinstance(event, OpenPositionMarkEventV1):
                self.open_positions[event.security_id] = event.shares
        portfolio_state = {
            "cash": str(equity_point.cash_base),
            "positions": [
                {"security_id": security_id, "shares": str(shares)}
                for security_id, shares in sorted(self.open_positions.items())
            ],
        }
        self.repository.write_backtest_staging(
            self.state.job_id,
            claim_token=self.state.claim_token,
            expected_version=self.state.status_version,
            state_schema_version=_PORTFOLIO_STATE_SCHEMA_VERSION,
            portfolio_state=portfolio_state,
            events=tuple(self.events),
            equity_curve=tuple(self.equity_curve),
            final_cash_base=equity_point.cash_base,
            initial_entry_selection=self.initial_entry_selection,
            lease=self.lease,
        )


def _connect(path: Path):
    return lambda: db.connect(path)


def _read_only_connect(path: Path):
    return lambda: sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _peak_rss_mib() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value / (1024 * 1024) if platform.system() == "Darwin" else value / 1024


def _number(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"expected numeric benchmark value, got {type(value).__name__}")


def _integer(value: object) -> int:
    if isinstance(value, int):
        return value
    raise TypeError(f"expected integer benchmark value, got {type(value).__name__}")


def _clone_database(source: Path, target: Path) -> None:
    try:
        subprocess.run(["cp", "-c", str(source), str(target)], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        shutil.copy2(source, target)


def _source_runs(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """SELECT r.id, r.strategy_id, r.parameters_json,
                      m.canonical_manifest_json
               FROM strategy_runs r
               JOIN strategy_jobs j ON j.id=r.id
               JOIN run_input_manifests m ON m.digest=r.run_input_manifest_digest
               WHERE j.status='complete'
                 AND r.start_month='2025-01' AND r.end_month='2026-07'
                 AND r.id LIKE 'benchmark-601-staging-%'"""
        ).fetchall()
    found: dict[str, str] = {}
    for raw_id, strategy_id, parameters_json, manifest_json in rows:
        manifest = json.loads(str(manifest_json))
        if len(manifest.get("securities", ())) != 738:
            continue
        parameters = json.loads(str(parameters_json))
        for label, expected_strategy_id in STRATEGY_IDS.items():
            if strategy_id != expected_strategy_id:
                continue
            upgrade = parameters.get("enable_position_upgrade") is True
            expected_upgrade = label.endswith("upgrade enabled")
            if upgrade != expected_upgrade and expected_strategy_id in {
                STRATEGY_IDS["Minervini"],
                STRATEGY_IDS["Weinstein"],
            }:
                continue
            if label not in found and "retry" not in str(raw_id):
                found[label] = str(raw_id)
    missing = [label for label in MODE_ORDER if label not in found]
    if missing:
        raise RuntimeError(f"pinned 738-security source runs missing: {missing}")
    return found


def _project_manifests(path: Path, sources: dict[str, str]) -> dict[str, str]:
    contract = current_execution_contract_payload(config.ROOT_DIR)
    execution_digest = current_execution_contract_digest(config.ROOT_DIR)
    projected: dict[str, str] = {}
    with sqlite3.connect(path) as conn:
        for source_id in sources.values():
            row = conn.execute(
                "SELECT m.canonical_manifest_json FROM strategy_runs r "
                "JOIN run_input_manifests m ON m.digest=r.run_input_manifest_digest "
                "WHERE r.id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"source manifest is missing: {source_id}")
            payload = json.loads(str(row[0]))
            payload.update(
                {
                    "engine_version": ENGINE_VERSION,
                    "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
                    **contract,
                }
            )
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            manifest = read_run_input_manifest(canonical)
            canonical = manifest.canonical_json()
            digest = manifest.digest()
            conn.execute(
                """INSERT OR IGNORE INTO run_input_manifests (
                           digest, execution_contract_digest,
                           canonical_manifest_json, created_at, manifest_version
                       ) VALUES (?, ?, ?, ?, ?)""",
                (
                    digest,
                    execution_digest,
                    canonical,
                    datetime.now(timezone.utc).isoformat(),
                    payload["schema_version"],
                ),
            )
            projected[source_id] = digest
    return projected


def _worker_lease(path: Path) -> WorkerLeaseFenceV1:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT instance_id, generation FROM strategy_worker_lease WHERE singleton_id=1"
        ).fetchone()
    if row is None:
        raise RuntimeError("pinned database has no worker lease")
    return WorkerLeaseFenceV1(instance_id=str(row[0]), generation=int(row[1]))


def _clone_job(
    path: Path,
    source_id: str,
    label: str,
    staging_mode: str,
    manifest_digest: str,
    execution_digest: str,
) -> str:
    job_id = f"benchmark-619-{staging_mode}-{label}-{uuid4()}"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        source_parent = conn.execute(
            "SELECT source_preparation_job_id FROM strategy_runs WHERE id=?",
            (source_id,),
        ).fetchone()
        if source_parent is None or source_parent[0] is None:
            raise RuntimeError(f"V2 source run has no preparation lineage: {source_id}")
        sequence = int(
            conn.execute(
                "SELECT COALESCE(MAX(enqueue_seq), 0) + 1 FROM strategy_jobs"
            ).fetchone()[0]
        )
        conn.execute(
            """INSERT INTO strategy_jobs (
                       id, job_type, status, parent_job_id, enqueue_seq,
                       claim_token, current_month, current_stage,
                       owner_instance_id, lease_generation, status_version,
                       cancel_requested_at, failure_code, failed_month,
                       failure_detail, deleted_at, audit_summary,
                       created_at, updated_at
                   ) VALUES (?, 'backtest', 'queued', ?, ?, NULL, NULL, NULL,
                             NULL, NULL, 1, NULL, NULL, NULL, NULL, NULL,
                             NULL, ?, ?)""",
            (job_id, source_parent[0], sequence, now, now),
        )
        conn.execute(
            """INSERT INTO strategy_runs (
                       id, strategy_id, strategy_api_version,
                       strategy_source_digest, parameters_json, profile_hash,
                       start_month, end_month, ordered_month_digest,
                       base_currency, starting_capital,
                          run_input_manifest_digest, execution_contract_digest,
                          created_at, manifest_version, run_universe_digest,
                          source_preparation_job_id, selection_json
                   )
                   SELECT ?, strategy_id, strategy_api_version,
                          strategy_source_digest, parameters_json, profile_hash,
                          start_month, end_month, ordered_month_digest,
                          base_currency, starting_capital,
                          ?, ?, ?, manifest_version, run_universe_digest,
                          NULL, selection_json
                   FROM strategy_runs WHERE id=?""",
            (job_id, manifest_digest, execution_digest, now, source_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise RuntimeError(f"source run not cloned: {source_id}")
    return job_id


def _staging_rows(repo: BacktestRepository, run_id: str) -> int:
    with repo._connect() as conn:  # benchmark-only inspection of temp state
        return sum(
            int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            for table in (
                "backtest_staging",
                "backtest_staging_batches",
                "backtest_staging_entry_selection",
                "backtest_staging_entry_selection_decisions",
            )
        )


def _result_projection(result: Any) -> dict[str, object]:
    return {
        "strategy_id": result.strategy_id,
        "parameters": result.parameters,
        "period": (result.start_month, result.end_month),
        "manifest_digest": result.run_input_manifest_digest,
        "execution_contract_digest": result.execution_contract_digest,
        "metrics": result.metrics.model_dump(mode="json"),
        "metric_availability": result.metric_availability.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in result.events],
        "equity_curve": [
            point.model_dump(mode="json") for point in result.equity_curve
        ],
        "final_cash_base": str(result.final_cash_base),
        "initial_entry_selection": (
            None
            if result.initial_entry_selection is None
            else result.initial_entry_selection.model_dump(mode="json")
        ),
    }


def _run_one(
    *,
    database: Path,
    historical_database: Path,
    source_id: str,
    label: str,
    staging_mode: str,
    lease: WorkerLeaseFenceV1,
    manifest_digest: str,
    execution_digest: str,
) -> dict[str, object]:
    job_id = _clone_job(
        database,
        source_id,
        label,
        staging_mode,
        manifest_digest,
        execution_digest,
    )
    repo = _MeasuredRepository(_connect(database), staging_mode=staging_mode)
    claim = repo.claim_next_strategy_job(lease=lease)
    if claim is None or claim.job.id != job_id or claim.backtest is None:
        raise RuntimeError(
            f"could not claim benchmark job for {label} ({staging_mode})"
        )
    prices = HistoricalPriceRepository(_read_only_connect(historical_database))
    engine = BacktestExecutionEngine(
        repository=repo,
        backtest=claim.backtest,
        prices=prices,
        project_root=config.ROOT_DIR,
        lease=lease,
    )
    original_sink = worker_module._StagingSink
    if staging_mode == "full-replace":
        worker_module._StagingSink = _LegacyStagingSink  # type: ignore[assignment]
    started = perf_counter()
    try:
        result = engine.run(job_id, claim.claim_token)
    finally:
        worker_module._StagingSink = original_sink
    wall_seconds = perf_counter() - started
    if result.status is not StrategyJobStatus.COMPLETE:
        failed = repo.strategy_job(job_id)
        raise RuntimeError(
            f"benchmark {label} ({staging_mode}) ended {result.status}: "
            f"{failed.failure_detail}; repository error: {repo.last_error}"
        )
    completed = repo.backtest_result(job_id)
    with repo._connect() as conn:
        result_digest = str(
            conn.execute(
                "SELECT result_digest FROM backtest_results WHERE run_id=?", (job_id,)
            ).fetchone()[0]
        )
    return {
        "mode": staging_mode,
        "wall_seconds": wall_seconds,
        "append_calls": repo.append_calls,
        "compressed_bytes": repo.compressed_bytes,
        "uncompressed_bytes": repo.uncompressed_bytes,
        "promotion_seconds": repo.promotion_seconds,
        "peak_rss_mib": _peak_rss_mib(),
        "sessions_processed": len(completed.equity_curve),
        "events": len(completed.events),
        "equity_points": len(completed.equity_curve),
        "result_digest": result_digest,
        "result": _result_projection(completed),
        "residual_staging_rows": _staging_rows(repo, job_id),
    }


def _markdown(
    results: dict[str, dict[str, dict[str, object]]], *, database: Path
) -> str:
    candidate_total = sum(
        _number(results[label]["append-batch"]["wall_seconds"]) for label in MODE_ORDER
    )
    baseline_total = sum(
        _number(results[label]["full-replace"]["wall_seconds"]) for label in MODE_ORDER
    )
    equivalent = all(
        results[label]["full-replace"]["result"]
        == results[label]["append-batch"]["result"]
        for label in MODE_ORDER
    )
    clean = all(
        _integer(results[label]["append-batch"]["residual_staging_rows"]) == 0
        for label in MODE_ORDER
    )
    candidate_beats_control = candidate_total < baseline_total
    under_reference = candidate_total < REFERENCE_SECONDS
    gate_passed = candidate_total < REFERENCE_SECONDS and equivalent and clean
    rows = [
        "---",
        "story: 616.3",
        "issue: 619",
        f"date: {datetime.now(timezone.utc).date().isoformat()}",
        f"status: {'complete' if gate_passed else 'review'}",
        f"gate_passed: {str(gate_passed).lower()}",
        "---",
        "",
        "# GH-619 incremental backtest staging benchmark",
        "",
        f"Temporary database clone: `{database}`; historical evidence was opened read-only.",
        "No provider or network access was used.",
        "Both implementations used the same pinned 738-security manifests and 406-session input.",
        "",
        "## Aggregate gate",
        "",
        "| Implementation | Wall seconds | Delta versus #613 reference |",
        "| --- | ---: | ---: |",
        f"| Full-replace control | {baseline_total:.3f} | {baseline_total - REFERENCE_SECONDS:+.3f} |",
        f"| Append-batch candidate | {candidate_total:.3f} | {candidate_total - REFERENCE_SECONDS:+.3f} |",
        f"| #613 reference | {REFERENCE_SECONDS:.3f} | 0.000 |",
        "",
        f"Aggregate gate: **{'PASS' if gate_passed else 'FAIL'}** (under #613 reference: {under_reference}; faster than same-run control: {candidate_beats_control}; exact output equivalence: {equivalent}; zero residual staging: {clean}).",
        "",
        "## Per-mode evidence",
        "",
        "| Mode | Control s | Candidate s | Delta s | Candidate appends | Candidate compressed bytes | Candidate uncompressed bytes | Promotion s | RSS MiB | Events | Equity | Exact | Residual |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: |",
    ]
    for label in MODE_ORDER:
        control = results[label]["full-replace"]
        candidate = results[label]["append-batch"]
        rows.append(
            f"| {label} | {_number(control['wall_seconds']):.3f} | {_number(candidate['wall_seconds']):.3f} | "
            f"{_number(candidate['wall_seconds']) - _number(control['wall_seconds']):+.3f} | "
            f"{candidate['append_calls']} | {candidate['compressed_bytes']} | {candidate['uncompressed_bytes']} | "
            f"{_number(candidate['promotion_seconds']):.3f} | {_number(candidate['peak_rss_mib']):.1f} | "
            f"{candidate['events']} | {candidate['equity_points']} | "
            f"{'yes' if control['result'] == candidate['result'] else 'no'} | {candidate['residual_staging_rows']} |"
        )
    rows.extend(
        [
            "",
            "## Interpretation",
            "",
            "The control uses the pre-616 cumulative in-memory sink and full-replace staging writer. The candidate uses the production `_StagingSink` and append-only session batches. `uncompressed_bytes` includes serialized checkpoint state plus the session payload(s) written by each append; control bytes are the full checkpoint, event, and equity arrays written on each replacement. Candidate compressed bytes are the zlib payload bytes.",
            "",
            f"All candidate runs processed 406 sessions: {all(_integer(results[label]['append-batch']['sessions_processed']) == 406 for label in MODE_ORDER)}.",
            f"All candidate staging cleanup was empty: {clean}.",
            f"All mode outputs were exactly equivalent: {equivalent}.",
            "",
        ]
    )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-database", type=Path, default=config.BACKTEST_DB)
    parser.add_argument(
        "--historical-database", type=Path, default=config.HISTORICAL_PRICE_CACHE
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-dir", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--mode", action="append", choices=MODE_ORDER)
    parser.add_argument("--staging-mode", choices=("full-replace", "append-batch"))
    args = parser.parse_args(argv)
    modes = tuple(args.mode or MODE_ORDER)
    sources = _source_runs(args.backtest_database)
    lease = _worker_lease(args.backtest_database)
    with tempfile.TemporaryDirectory(prefix="benchmark-619-", dir=args.work_dir) as raw:
        database = Path(raw) / "backtest.db"
        _clone_database(args.backtest_database, database)
        projected = _project_manifests(database, sources)
        execution_digest = current_execution_contract_digest(config.ROOT_DIR)
        results: dict[str, dict[str, dict[str, object]]] = {}
        for label in modes:
            results[label] = {}
            staging_modes = (
                (args.staging_mode,)
                if args.staging_mode is not None
                else ("full-replace", "append-batch")
            )
            for staging_mode in staging_modes:
                results[label][staging_mode] = _run_one(
                    database=database,
                    historical_database=args.historical_database,
                    source_id=sources[label],
                    label=label,
                    staging_mode=staging_mode,
                    lease=lease,
                    manifest_digest=projected[sources[label]],
                    execution_digest=execution_digest,
                )
    if len(modes) != len(MODE_ORDER):
        summary = {
            label: {
                staging_mode: {
                    key: value for key, value in measurement.items() if key != "result"
                }
                for staging_mode, measurement in values.items()
            }
            for label, values in results.items()
        }
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        return 0
    output = args.output or (
        Path("_bmad-output/implementation-artifacts")
        / f"benchmark-619-{datetime.now(timezone.utc).date().isoformat()}.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_markdown(results, database=database), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
