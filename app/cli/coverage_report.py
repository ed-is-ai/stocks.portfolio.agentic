"""CLI: per-month yfinance price coverage of the point-in-time S&P 500 (#74).

Usage::

    uv run python -m app.cli.coverage_report sp500 [--from YYYY-MM]
        [--to YYYY-MM] [--month YYYY-MM] [--csv PATH]
        [--membership-db PATH] [--price-db PATH] [--wiki-db PATH]
        [--overrides PATH]

Prints one line per month and yearly totals, or the missing, not cached,
WIKI-priced and suspect tickers of ``--month``. Index leavers yfinance does
not price but the WIKI archive (#70) does count as ``wiki``; without
``--wiki-db`` the group stays empty and the footer says so. ``--csv`` writes
the full table. Without ``--to`` the report ends at the last complete month
in the price cache. Both databases are opened read-only and must already
exist.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from datetime import date
from itertools import groupby
from pathlib import Path

from app.core.config import (
    HISTORICAL_PRICE_CACHE,
    INDEX_MEMBERSHIP_DB,
    WIKI_PRICES_DB,
)
from app.repositories.index_membership_repo import IndexMembershipRepository
from app.services.index_membership.coverage import (
    AS_OF_RULE,
    MonthCoverage,
    coverage_report,
    last_complete_month,
    price_spans,
    read_only,
)
from app.services.index_membership.sp500_import import INDEX_ID
from app.services.index_membership.wiki_link import OVERRIDES_PATH, wiki_coverage

START_MONTH = "2000-01"
LIMIT_NOTE = (
    "limits: a ticker reused outside the index (e.g. T: one interval, but its"
    " 1996-2005 prices are SBC's) is counted priced, not suspect; a company"
    " that left and rejoined is counted suspect; missing includes index"
    " leavers that still trade (market-cap removals)."
)


def month_arg(value: str) -> str:
    """Validate a ``YYYY-MM`` argument."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}")
    return value


def main(argv: list[str] | None = None) -> None:
    """Print the coverage report (exits non-zero on a missing DB or import)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.month and (args.start or args.end):
        parser.error("--month cannot be combined with --from/--to")
    for path in (args.membership_db, args.price_db):
        if not path.exists():
            raise SystemExit(f"database not found: {path}")
    if args.csv and not args.csv.parent.is_dir():
        raise SystemExit(f"csv directory not found: {args.csv.parent}")
    repo = IndexMembershipRepository(lambda: read_only(args.membership_db))
    if repo.latest_import(INDEX_ID) is None:
        raise SystemExit(f"no {INDEX_ID} membership imported in {args.membership_db}")
    try:
        spans = price_spans(args.price_db)
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"cannot read price cache {args.price_db}: {exc}") from exc
    try:
        wiki = wiki_coverage(args.wiki_db, args.overrides)
    except (ValueError, sqlite3.OperationalError) as exc:
        raise SystemExit(f"cannot read WIKI prices: {exc}") from exc
    end = args.end or last_complete_month(spans, date.today())
    start = args.start or START_MONTH
    start, end = (args.month, args.month) if args.month else (start, end)
    if start > end:
        raise SystemExit(f"--from {start} is after the end month {end}")
    rows = coverage_report(repo, spans, start, end, wiki)
    if args.csv:
        write_csv(rows, args.csv)
    if args.month:
        print_detail(rows[0])
    else:
        print_table(rows)
    print(LIMIT_NOTE)
    if wiki is None:
        print(f"WIKI prices not loaded ({args.wiki_db}): wiki counts are 0.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", choices=[INDEX_ID])
    parser.add_argument(
        "--from", dest="start", type=month_arg, help=f"Default {START_MONTH}."
    )
    parser.add_argument("--to", dest="end", type=month_arg)
    parser.add_argument("--month", type=month_arg, help="Detail of one month.")
    parser.add_argument("--csv", type=Path, help="Write the full table here.")
    parser.add_argument("--membership-db", type=Path, default=INDEX_MEMBERSHIP_DB)
    parser.add_argument("--price-db", type=Path, default=HISTORICAL_PRICE_CACHE)
    parser.add_argument("--wiki-db", type=Path, default=WIKI_PRICES_DB)
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_PATH)
    return parser


def _line(row: MonthCoverage) -> str:
    flags = " ".join(
        f for f, on in (("low", row.confidence == "low"), ("stale", row.stale)) if on
    )
    pct = 100 * row.priced / row.members if row.members else 0.0
    return (
        f"{row.month} as_of={row.as_of} members={row.members} priced={row.priced}"
        f" ({pct:.1f}%) suspect={row.suspect} wiki={row.wiki}"
        f" not_cached={row.not_cached} missing={row.missing} {flags}"
    ).rstrip()


def print_table(rows: list[MonthCoverage]) -> None:
    """Print one line per month, then member-month totals per year."""
    print(f"roster as of the {AS_OF_RULE} of each month")
    for row in rows:
        print(_line(row))
    print("yearly totals (member-months):")
    for year, group in groupby(rows, key=lambda r: r.month[:4]):
        months = list(group)
        members = sum(r.members for r in months)
        priced = sum(r.priced for r in months)
        pct = 100 * priced / members if members else 0.0
        print(
            f"{year} members={members} priced={priced} ({pct:.1f}%)"
            f" suspect={sum(r.suspect for r in months)}"
            f" wiki={sum(r.wiki for r in months)}"
            f" not_cached={sum(r.not_cached for r in months)}"
            f" missing={sum(r.missing for r in months)}"
        )


def print_detail(row: MonthCoverage) -> None:
    """Print one month's line and its missing, not cached, wiki and suspect
    tickers."""
    print(_line(row))
    print(f"missing: {' '.join(row.missing_tickers)}")
    print(f"not cached: {' '.join(row.not_cached_tickers)}")
    print(f"wiki: {' '.join(row.wiki_tickers)}")
    print(f"suspect: {' '.join(row.suspect_tickers)}")


def write_csv(rows: list[MonthCoverage], path: Path) -> None:
    """Write every month's coverage, tickers space-separated."""
    fields = list(MonthCoverage.model_fields)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            record = row.model_dump()
            record["missing_tickers"] = " ".join(row.missing_tickers)
            record["suspect_tickers"] = " ".join(row.suspect_tickers)
            record["wiki_tickers"] = " ".join(row.wiki_tickers)
            record["not_cached_tickers"] = " ".join(row.not_cached_tickers)
            writer.writerow(record)


if __name__ == "__main__":
    main()
