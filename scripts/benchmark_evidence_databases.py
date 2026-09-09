"""Run via python -m scripts.benchmark_evidence_databases on offline copies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.repositories.evidence_benchmark import (
    benchmark_metadata,
    database_inventory,
    result_component_timings,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-db", type=Path, required=True)
    parser.add_argument("--backtest-db", type=Path, required=True)
    parser.add_argument(
        "--code-revision",
        required=True,
        help="Exact application revision, with a dirty suffix if modified",
    )
    parser.add_argument(
        "--snapshot-id",
        required=True,
        help="Identifier recorded with the offline backup metadata",
    )
    parser.add_argument("--profile-hash")
    parser.add_argument("--run-id")
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    # Time before inventory scans warm the evidence pages.
    report: dict[str, object] = {
        "metadata": benchmark_metadata(
            code_revision=args.code_revision,
            snapshot_id=args.snapshot_id,
        )
    }
    if args.profile_hash or args.run_id:
        report["result_components"] = result_component_timings(
            args.backtest_db,
            profile_hash=args.profile_hash,
            run_id=args.run_id,
            repetitions=args.repetitions,
        )
    report["historical"] = database_inventory(args.historical_db)
    report["backtest"] = database_inventory(args.backtest_db)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
