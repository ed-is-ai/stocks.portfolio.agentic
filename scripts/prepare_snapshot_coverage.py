"""Prepare durable verified coverage in an explicitly selected writable database.

Run before serving traffic after deployment or evidence changes. This is a
maintenance command: it initializes schema and writes derived coverage summaries.
It does not run providers or start workers. Back up operational databases first.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest-db", type=Path, required=True)
    parser.add_argument("--profile-hash", help="Defaults to the active profile")
    args = parser.parse_args()
    path = args.backtest_db.resolve(strict=True)
    if not path.is_file():
        parser.error("--backtest-db must be an existing SQLite file")
    repository = BacktestRepository(db.make_connect(lambda: str(path)))
    start = time.perf_counter()
    repository.ensure_schema()
    summary = repository.prepare_snapshot_coverage(args.profile_hash)
    print(
        json.dumps(
            {
                "database": str(path),
                "preparation_seconds": time.perf_counter() - start,
                "summary": summary.model_dump(mode="json"),
                "durable": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
