"""Read-only inventory and component timings for offline evidence databases."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
import platform
import sqlite3
from statistics import median
from time import perf_counter
from typing import Callable, TypeVar

from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.db import Connect

T = TypeVar("T")


def readonly_connect(path: Path) -> Connect:
    """Never create a missing input or initialize/migrate its schema."""
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("benchmark input must be a database file")

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    return connect


def database_inventory(path: Path) -> dict[str, object]:
    """Count stored rows and report physical SQLite object sizes, if supported.

    Full counts/dbstat intentionally scan evidence: this is an offline
    baseline tool, never part of a request or scheduled worker.
    """
    path = path.resolve(strict=True)
    with closing(readonly_connect(path)()) as conn:
        conn.execute("BEGIN")
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        object_bytes = None
        try:
            object_bytes = dict(
                conn.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name")
            )
        except sqlite3.OperationalError as exc:
            if "no such table: dbstat" not in str(exc):
                raise
        return {
            "database": path.name,
            "file_bytes": path.stat().st_size,
            "wal_bytes": Path(str(path) + "-wal").stat().st_size
            if Path(str(path) + "-wal").exists()
            else 0,
            "logical_bytes": page_size * page_count,
            "free_bytes": page_size * freelist,
            "journal_mode": journal,
            "row_counts": counts,
            "object_bytes": object_bytes,
            "dbstat_available": object_bytes is not None,
        }


def _summary(values: list[float]) -> dict[str, object]:
    return {
        "samples_seconds": values,
        "median_seconds": median(values),
        "p95_seconds": sorted(values)[ceil(len(values) * 0.95) - 1],
    }


def result_component_timings(
    path: Path,
    *,
    profile_hash: str | None = None,
    run_id: str | None = None,
    repetitions: int = 5,
) -> dict[str, object]:
    """Measure first repository use and repeated use without claiming OS cold.

    Measures the Result's expensive repository components, not HTML rendering
    or HTTP latency. A run selects its pinned profile and start month.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if profile_hash is None and run_id is None:
        raise ValueError("select a profile hash or completed run ID")
    repo = BacktestRepository(readonly_connect(path))
    samples: dict[str, list[float]] = {}
    failures: list[dict[str, object]] = []
    selected_profile = profile_hash

    def timed(name: str, operation: Callable[[], T]) -> T | None:
        started = perf_counter()
        try:
            return operation()
        except BacktestIntegrityError as exc:
            failures.append(
                {
                    "component": name,
                    "sample": len(samples.get(name, [])),
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
            )
            return None
        finally:
            samples.setdefault(name, []).append(perf_counter() - started)

    for _ in range(repetitions + 1):
        started = perf_counter()
        if run_id is not None:
            result = timed("result_lookup", lambda: repo.backtest_result(run_id))
            if result is None:
                break
            # Result types stay owned by the existing repository.
            selected_profile = result.profile_hash
            start_month = result.start_month
            if profile_hash is not None and profile_hash != selected_profile:
                raise ValueError("selected profile does not match the pinned run")
        else:
            selected_profile = profile_hash
            start_month = None
        assert selected_profile is not None
        timed("coverage", lambda: repo.snapshot_coverage(selected_profile))
        timed("roster", lambda: repo.roster_member_identities(selected_profile))
        if start_month is not None:
            timed(
                "member_revision_verification",
                lambda: repo.snapshot_member_revisions(selected_profile, start_month),
            )
        samples.setdefault("total_components", []).append(perf_counter() - started)
    return {
        "status": "integrity_failure" if failures else "ok",
        "failures": failures,
        "profile_hash": selected_profile,
        "run_id": run_id,
        "requested_warm_repetitions": repetitions,
        "warm_repetitions": max(0, len(next(iter(samples.values()), [])) - 1),
        "first_use_seconds": {name: values[0] for name, values in samples.items()},
        "warm": {
            name: _summary(values[1:])
            for name, values in samples.items()
            if len(values) > 1
        },
        "cache_conditions": (
            "First-use means a new repository cache, not a cold OS page cache. "
            "Warm samples reuse that repository. Component totals exclude HTML, "
            "HTTP and presentation; these are not Result-rendering acceptance results. "
            "When integrity fails, durations measure rejection, not successful reads."
        ),
    }


def benchmark_metadata(*, code_revision: str, snapshot_id: str) -> dict[str, str]:
    return {
        "application_revision": code_revision,
        "input_snapshot_id": snapshot_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "percentile_method": "nearest-rank",
    }
