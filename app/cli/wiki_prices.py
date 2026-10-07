"""CLI: the Quandl WIKI price archive as a separate price source (#70).

Usage::

    uv run python -m app.cli.wiki_prices import --csv PATH [--wiki-db PATH]
    uv run python -m app.cli.wiki_prices validate [--year Y]
        [--wiki-db PATH] [--price-db PATH]
    uv run python -m app.cli.wiki_prices unmatched [--from YYYY-MM]
        [--to YYYY-MM] [--membership-db PATH] [--price-db PATH]
        [--wiki-db PATH] [--overrides PATH]

``import`` loads the CSV into ``wiki_prices.db`` (a file with an already
imported digest writes nothing). ``validate`` compares WIKI closes with the
yfinance cache on identical dates of one year. ``unmatched`` lists membership
intervals whose months neither source prices, most months first (``--to``
defaults to WIKI's last complete month). Only ``import`` writes, and only to
the WIKI database.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path

from app.cli.coverage_report import START_MONTH, month_arg
from app.core.config import HISTORICAL_PRICE_CACHE, INDEX_MEMBERSHIP_DB, WIKI_PRICES_DB
from app.repositories import db
from app.repositories.index_membership_repo import IndexMembershipRepository
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.index_membership.coverage import (
    coverage_report,
    last_complete_month,
    price_spans,
    read_only,
)
from app.services.index_membership.wiki_link import (
    AGREE_WITHIN,
    OUTLIER_GAP,
    OVERRIDES_PATH,
    unmatched,
    validate,
    wiki_coverage,
)

#: WIKI ends 2018-03-27, so 2017 is its last full sample year.
DEFAULT_YEAR = 2017


def main(argv: list[str] | None = None) -> None:
    """Run one command (exits non-zero on a bad file or missing database)."""
    args = _parser().parse_args(argv)
    try:
        args.run(args)
    except (ValueError, sqlite3.OperationalError) as exc:
        raise SystemExit(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    load = commands.add_parser("import")
    load.add_argument("--csv", type=Path, required=True)
    load.set_defaults(run=run_import)
    check = commands.add_parser("validate")
    check.add_argument("--year", type=int, default=DEFAULT_YEAR)
    check.set_defaults(run=run_validate)
    gaps = commands.add_parser("unmatched")
    gaps.add_argument("--from", dest="start", type=month_arg, default=START_MONTH)
    gaps.add_argument("--to", dest="end", type=month_arg)
    gaps.add_argument("--membership-db", type=Path, default=INDEX_MEMBERSHIP_DB)
    gaps.add_argument("--overrides", type=Path, default=OVERRIDES_PATH)
    gaps.set_defaults(run=run_unmatched)
    for command in (load, check, gaps):
        command.add_argument("--wiki-db", type=Path, default=WIKI_PRICES_DB)
    for command in (check, gaps):
        command.add_argument("--price-db", type=Path, default=HISTORICAL_PRICE_CACHE)
    return parser


def run_import(args: argparse.Namespace) -> None:
    """Import ``--csv`` and print the import record."""
    if not args.csv.is_file():
        raise SystemExit(f"csv not found: {args.csv}")
    repo = WikiPriceRepository(db.make_connect(lambda: args.wiki_db))
    repo.ensure_schema()
    record, created = repo.import_csv(args.csv)
    print("imported" if created else "already imported (nothing written)")
    for field, value in record.model_dump().items():
        print(f"{field}: {value}")


def run_validate(args: argparse.Namespace) -> None:
    """Print the agreement share and the outlier tickers of ``--year``."""
    _require(args.wiki_db, args.price_db)
    result = validate(
        WikiPriceRepository(lambda: read_only(args.wiki_db)), args.price_db, args.year
    )
    share = 100 * result.agreeing / result.dates if result.dates else 0.0
    print(
        f"{result.year}: {result.tickers} tickers, {result.dates} dates,"
        f" {share:.1f}% of closes within {AGREE_WITHIN:.0%} of yfinance"
    )
    print(
        f"yfinance adjustment since {result.year} (splits, spin-offs; WIKI is"
        f" as traded): {len(result.factors)} tickers"
    )
    print("  " + " ".join(f"{t} x{f:.2f}" for t, f in result.factors))
    print(f"median gap > {OUTLIER_GAP:.0%}: {len(result.outliers)} tickers")
    for ticker, gap in result.outliers:
        print(f"  {ticker} {gap:.1%}")


def run_unmatched(args: argparse.Namespace) -> None:
    """Print intervals whose months stay missing with WIKI loaded."""
    _require(args.membership_db, args.price_db, args.wiki_db)
    covers = wiki_coverage(args.wiki_db, args.overrides)
    repo = IndexMembershipRepository(lambda: read_only(args.membership_db))
    wiki_spans = WikiPriceRepository(lambda: read_only(args.wiki_db)).ticker_spans()
    end = args.end or last_complete_month(wiki_spans, date.today())
    if args.start > end:
        raise SystemExit(f"--from {args.start} is after the end month {end}")
    rows = coverage_report(repo, price_spans(args.price_db), args.start, end, covers)
    found = unmatched(repo, rows)
    print(f"{args.start}..{end}: {len(found)} intervals with missing months")
    for item in found:
        print(
            f"{item.ticker} {item.start_date}..{item.end_date or ''}"
            f" missing_months={item.missing_months}"
        )


def _require(*paths: Path) -> None:
    for path in paths:
        if not path.exists():
            raise SystemExit(f"database not found: {path}")


if __name__ == "__main__":
    main()
