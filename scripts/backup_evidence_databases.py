"""Create consistent SQLite backup copies for an offline evidence rehearsal."""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3


def backup(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        src.execute("BEGIN")
        with closing(sqlite3.connect(destination)) as dst:
            last_remaining = None

            def progress(_status: int, remaining: int, total: int) -> None:
                nonlocal last_remaining
                if remaining != last_remaining and (remaining == 0 or remaining % 81920 == 0):
                    print(f"{source.name}: {total - remaining}/{total} pages", flush=True)
                last_remaining = remaining

            src.backup(dst, pages=8192, progress=progress)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-source", type=Path, required=True)
    parser.add_argument("--historical-destination", type=Path, required=True)
    parser.add_argument("--backtest-source", type=Path, required=True)
    parser.add_argument("--backtest-destination", type=Path, required=True)
    parser.add_argument("--skip-historical", action="store_true")
    args = parser.parse_args()
    for source in (args.historical_source, args.backtest_source):
        if not source.is_file():
            parser.error(f"source database does not exist: {source}")
    if not args.skip_historical:
        backup(args.historical_source.resolve(), args.historical_destination.resolve())
    backup(args.backtest_source.resolve(), args.backtest_destination.resolve())


if __name__ == "__main__":
    main()
