"""Measure navigation against a disposable SQLite backup, with workers disabled.

Run with python -m scripts.benchmark_strategy_navigation. Timings exclude backup
and include real route rendering. This measures navigation, not Result integrity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest-db", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 2:
        parser.error("--repetitions must be at least 2 for first/warm measurements")
    if args.output.exists() and args.output.samefile(args.backtest_db):
        parser.error("--output must differ from the source database")

    os.environ["STRATEGY_MANAGER_WORKER_ENABLED"] = "false"
    from fastapi.testclient import TestClient

    from app.api.app import app
    from app.core import config

    with tempfile.TemporaryDirectory(prefix="strategy-navigation-") as directory:
        backup = Path(directory) / "backtest.db"
        source = sqlite3.connect(
            args.backtest_db.resolve().as_uri() + "?mode=ro", uri=True
        )
        destination = sqlite3.connect(backup)
        try:
            source.execute("BEGIN")
            source.execute("SELECT count(*) FROM sqlite_master").fetchone()
            source.backup(destination)
            source_identity = {
                "size_bytes": args.backtest_db.stat().st_size,
                "schema_version": source.execute("PRAGMA schema_version").fetchone()[0],
            }
        finally:
            destination.close()
            source.close()
        config.BACKTEST_DB = backup
        # These screens read stored backtest evidence. An empty isolated price
        # cache matches the baseline and prevents access to live price storage.
        config.HISTORICAL_PRICE_CACHE = Path(directory) / "empty-prices.db"
        # Deliberately omit the context manager: no application lifespan/workers.
        client = TestClient(app)
        routes = {}
        try:
            for path in (
                "/strategy-manager",
                "/strategy-manager/initialization",
                "/strategy-manager/readiness",
                "/strategy-manager/configuration",
                "/strategy-manager/backtests",
            ):
                samples = []
                statuses = []
                outcomes = []
                for _ in range(args.repetitions):
                    start = time.perf_counter()
                    try:
                        response = client.get(path)
                        elapsed = time.perf_counter() - start
                        statuses.append(response.status_code)
                        outcomes.append(
                            {
                                "body_sha256": hashlib.sha256(
                                    response.content
                                ).hexdigest(),
                                "status_error_markers": response.text.count(
                                    "status-error"
                                ),
                                "alert_markers": response.text.count('role="alert"'),
                            }
                        )
                    except Exception as exc:
                        elapsed = time.perf_counter() - start
                        statuses.append(None)
                        outcomes.append({"exception_type": type(exc).__name__})
                    samples.append(elapsed)
                routes[path] = {
                    "seconds": samples,
                    "status_codes": statuses,
                    "first_seconds": samples[0],
                    "warm_median_seconds": statistics.median(samples[1:]),
                    "outcomes": outcomes,
                }
                print(path, routes[path], flush=True)
        finally:
            client.close()
        report = {
            "code_revision": args.code_revision,
            "snapshot_id": args.snapshot_id,
            "source_identity": source_identity,
            "repository_code_sha256": hashlib.sha256(
                (
                    Path(__file__).resolve().parents[1]
                    / "app/repositories/backtest_repo.py"
                ).read_bytes()
            ).hexdigest(),
            "method": "Sequential in-process GETs; disposable SQLite backup; no lifespan; "
            "empty isolated historical cache; OS caches uncontrolled. First means first "
            "visit to each route, not a cold process. HTTP 200 can contain readiness "
            "errors and is not proof of a successful completed Result.",
            "routes": routes,
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
