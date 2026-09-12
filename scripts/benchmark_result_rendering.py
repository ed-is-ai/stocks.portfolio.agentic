"""Benchmark one completed Result route on an offline SQLite copy.

Warm samples share one application process. Cold samples start a fresh process
for each request; application startup is excluded from the route timing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]


def _one_request(run_id: str, backtest_db: Path, historical_db: Path) -> dict[str, object]:
    os.environ["STRATEGY_MANAGER_WORKER_ENABLED"] = "false"
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.core import config

    config.BACKTEST_DB = backtest_db
    config.HISTORICAL_PRICE_CACHE = historical_db
    app = create_app(
        strategy_jobs_enabled=False,
        prepare_strategy_coverage=lambda: None,
    )
    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.get(f"/strategy-manager/results/{run_id}")
        elapsed = time.perf_counter() - started
    if response.status_code != 200:
        raise RuntimeError(f"Result route returned HTTP {response.status_code}")
    return {
        "seconds": elapsed,
        "status_code": response.status_code,
        "body_bytes": len(response.content),
        "body_sha256": hashlib.sha256(response.content).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile-hash", required=True)
    parser.add_argument("--backtest-db", type=Path, required=True)
    parser.add_argument("--historical-db", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--warm-repetitions", type=int, default=20)
    parser.add_argument("--cold-repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--single-cold", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.warm_repetitions < 2:
        parser.error("--warm-repetitions must be at least 2")
    if args.cold_repetitions < 1:
        parser.error("--cold-repetitions must be positive")

    if args.single_cold:
        print(
            json.dumps(
                _one_request(args.run_id, args.backtest_db, args.historical_db),
                sort_keys=True,
            )
        )
        return

    warmup = _one_request(args.run_id, args.backtest_db, args.historical_db)
    warm = [
        _one_request(args.run_id, args.backtest_db, args.historical_db)
        for _ in range(args.warm_repetitions)
    ]
    cold: list[dict[str, object]] = []
    for _ in range(args.cold_repetitions):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.benchmark_result_rendering",
                "--run-id",
                args.run_id,
                "--profile-hash",
                args.profile_hash,
                "--backtest-db",
                str(args.backtest_db),
                "--historical-db",
                str(args.historical_db),
                "--code-revision",
                args.code_revision,
                "--snapshot-id",
                args.snapshot_id,
                "--single-cold",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        cold.append(json.loads(completed.stdout))

    warm_seconds = [float(item["seconds"]) for item in warm]
    cold_seconds = [float(item["seconds"]) for item in cold]
    report = {
        "run_id": args.run_id,
        "profile_hash": args.profile_hash,
        "code_revision": args.code_revision,
        "snapshot_id": args.snapshot_id,
        "method": "HTTP GET of a completed Result; 20 warm samples in one process; "
        "5 cold samples in fresh processes; application startup excluded; OS page "
        "cache uncontrolled",
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "warmup": warmup,
        "warm": {
            "samples_seconds": warm_seconds,
            "median_seconds": statistics.median(warm_seconds),
            "p95_seconds": _p95(warm_seconds),
            "all_http_200": all(item["status_code"] == 200 for item in warm),
        },
        "cold": {
            "samples_seconds": cold_seconds,
            "median_seconds": statistics.median(cold_seconds),
            "p95_seconds": _p95(cold_seconds),
            "all_http_200": all(item["status_code"] == 200 for item in cold),
        },
    }
    rendered_hashes = {
        item["body_sha256"] for item in [*warm, *cold] if "body_sha256" in item
    }
    if len(rendered_hashes) != 1:
        raise RuntimeError("Result body changed between benchmark samples")
    report["rendered_body_sha256"] = rendered_hashes.pop()
    if args.output is None:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
