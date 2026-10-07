"""CLI: measure survivorship bias on an equal-weight S&P 500 (#75).

Usage::

    uv run python -m app.cli.survivorship_bias [--from YYYY-MM] [--to YYYY-MM]
        [--csv PATH] [--membership-db PATH] [--price-db PATH] [--wiki-db PATH]
        [--overrides PATH]

Compares equal-weight, monthly-rebalanced portfolios over months the WIKI
archive can price (default 2000-01 to 2018-02): today's S&P 500 members;
each month's point-in-time members priced by yfinance alone (the bias from
membership); the same with WIKI's delisted prices too (membership plus
delisted prices); and SPY for context. Every database is opened read-only.
Report only.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from app.cli.coverage_report import month_arg
from app.core.config import (
    HISTORICAL_PRICE_CACHE,
    INDEX_MEMBERSHIP_DB,
    WIKI_PRICES_DB,
)
from app.repositories.index_membership_repo import IndexMembershipRepository
from app.services.index_membership.coverage import (
    month_as_of,
    months_between,
    read_only,
)
from app.services.index_membership.sp500_import import INDEX_ID
from app.services.index_membership.survivorship import (
    MonthResult,
    Series,
    Summary,
    load_wiki,
    load_yf,
    monthly,
    portfolio,
    previous_month,
    summarise,
    with_terminal_price,
)
from app.services.index_membership.wiki_link import (
    OVERRIDES_PATH,
    load_overrides,
    wiki_ticker,
)

START_MONTH = "2000-01"
#: WIKI ends 2018-03-27, so February 2018 is its last complete month.
END_MONTH = "2018-02"


def main(argv: list[str] | None = None) -> None:
    """Print both portfolios' summaries and yearly returns."""
    args = _parser().parse_args(argv)
    for path in (args.membership_db, args.price_db, args.wiki_db):
        if not path.exists():
            raise SystemExit(f"database not found: {path}")
    if args.start > args.end:
        raise SystemExit(f"--from {args.start} is after --to {args.end}")
    repo = IndexMembershipRepository(lambda: read_only(args.membership_db))
    latest = repo.latest_import(INDEX_ID)
    if latest is None:
        raise SystemExit(f"no {INDEX_ID} membership imported in {args.membership_db}")
    months = months_between(args.start, args.end)
    today, point_in_time = _universes(repo, months, latest.last_date)
    with_wiki, yf_only = _returns(repo, args, months, today, point_in_time)
    results = {
        "today": portfolio(months, today, with_wiki),
        "pit_yfinance": portfolio(months, point_in_time, yf_only),
        "point_in_time": portfolio(months, point_in_time, with_wiki),
        "SPY": portfolio(months, {m: ["SPY"] for m in months}, with_wiki),
    }
    _print(results, months)
    if args.csv:
        _write_csv(results, args.csv)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", type=month_arg, default=START_MONTH)
    parser.add_argument("--to", dest="end", type=month_arg, default=END_MONTH)
    parser.add_argument("--csv", type=Path, help="Write monthly results here.")
    parser.add_argument("--membership-db", type=Path, default=INDEX_MEMBERSHIP_DB)
    parser.add_argument("--price-db", type=Path, default=HISTORICAL_PRICE_CACHE)
    parser.add_argument("--wiki-db", type=Path, default=WIKI_PRICES_DB)
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_PATH)
    return parser


def _universes(
    repo: IndexMembershipRepository, months: list[str], last_date: str
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return each month's ``today`` and point-in-time member tickers; the
    point-in-time roster is taken at the previous month's end, without
    reused tickers."""
    current = repo.roster_on(INDEX_ID, last_date)
    assert current is not None
    today_members = [m.ticker for m in current.members]
    point_in_time: dict[str, list[str]] = {}
    reused: dict[str, bool] = {}
    for month in months:
        roster = repo.roster_on(INDEX_ID, month_as_of(previous_month(month)))
        assert roster is not None
        for member in roster.members:
            if member.ticker not in reused:
                spells = repo.intervals_for(INDEX_ID, member.ticker)
                reused[member.ticker] = len(spells) > 1
        point_in_time[month] = [
            m.ticker for m in roster.members if not reused[m.ticker]
        ]
    return {m: today_members for m in months}, point_in_time


def _returns(
    repo: IndexMembershipRepository,
    args: argparse.Namespace,
    months: list[str],
    today: dict[str, list[str]],
    point_in_time: dict[str, list[str]],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Monthly gross returns per ticker, yfinance first then WIKI, and
    yfinance alone; each settled at a terminal price where one applies."""
    overrides = load_overrides(args.overrides)
    exits = {
        e.ticker: (e.exit_date, e.terminal_price)
        for e in repo.terminal_events(INDEX_ID)
        if e.terminal_price is not None
    }
    first_year, last_year = int(months[0][:4]) - 1, int(months[-1][:4])
    start, end = f"{previous_month(months[0])}-01", month_as_of(months[-1])
    tickers = {t for u in (today, point_in_time) for ms in u.values() for t in ms}
    returns: dict[str, dict[str, float]] = {}
    yf_only: dict[str, dict[str, float]] = {}
    for ticker in sorted(tickers | {"SPY"}):
        exit_info = exits.get(ticker)
        yf = load_yf(args.price_db, ticker.replace(".", "-"), first_year, last_year)
        merged = yf_only[ticker] = _settled(yf, exit_info)
        wiki_names = {
            wiki_ticker(ticker, month_as_of(m), overrides)
            for m in months
            if ticker in point_in_time.get(m, []) or ticker in today[m]
        }
        for name in sorted(wiki_names):
            wiki = _settled(load_wiki(args.wiki_db, name, start, end), exit_info)
            merged = {**wiki, **merged}
        returns[ticker] = merged
    return returns, yf_only


def _settled(
    series: Series, exit_info: tuple[str, float | None] | None
) -> dict[str, float]:
    found = monthly(series)
    if exit_info is None:
        return found
    return with_terminal_price(found, series, *exit_info)


def _print(results: dict[str, list[MonthResult]], months: list[str]) -> None:
    print(f"{months[0]}..{months[-1]}, equal weight, rebalanced monthly")
    summaries = {name: summarise(r) for name, r in results.items()}
    for name, s in summaries.items():
        print(_summary_line(name, s))
    today = summaries["today"].cagr
    membership = today - summaries["pit_yfinance"].cagr
    both = today - summaries["point_in_time"].cagr
    print(
        f"bias from membership alone (today - pit_yfinance): {membership:+.2%} a year"
    )
    print(f"bias with delisted prices (today - point_in_time): {both:+.2%} a year")
    print("year " + "".join(f"{name:>15}" for name in results))
    for year in sorted({m[:4] for m in months}):
        cells = "".join(f"{_year_return(r, year):+15.1%}" for r in results.values())
        print(f"{year} {cells}")


def _summary_line(name: str, s: Summary) -> str:
    return (
        f"{name:>13}: CAGR {s.cagr:+.2%} total {s.total:+.1%} max DD"
        f" {s.max_drawdown:.1%} (2000-02 {s.drawdown_2000_2002:.1%},"
        f" 2007-09 {s.drawdown_2007_2009:.1%}) coverage {s.coverage:.1%}"
    )


def _year_return(results: list[MonthResult], year: str) -> float:
    value = 1.0
    for r in results:
        if r.month.startswith(year):
            value *= r.gross
    return value - 1


def _write_csv(results: dict[str, list[MonthResult]], path: Path) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["portfolio", "month", "members", "priced", "gross"])
        for name, rows in results.items():
            for r in rows:
                writer.writerow([name, r.month, r.members, r.priced, f"{r.gross:.6f}"])


if __name__ == "__main__":
    main()
