"""Measure survivorship bias on an equal-weight S&P 500 (#75). Report only.

Two portfolios are rebalanced monthly to equal weights over the same months:

* ``today``: the members of the newest roster (today's list), as a backtest
  that reconstructs history from the current constituents would see them;
* ``point_in_time``: the members on the last calendar day of the previous
  month (``roster_on``), reused tickers excluded.

A member counts in a month when its price series has a session in both the
previous month and the month (it is held from the previous close). Total
returns use yfinance (split-adjusted close plus dividends) first, then the
WIKI archive (as-traded close, dividends and split ratios). A member whose
prices stop within ``DELISTED_WITHIN`` of its removal and that has a
terminal price (#73) exits at that price instead of its last close; after
its last session the position is cash for the rest of the month. Members
without prices are counted, never silently priced.
"""

import json
import sqlite3
import zlib
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.services.index_membership.coverage import read_only

#: Prices ending this close to a removal settle at its terminal price.
DELISTED_WITHIN = timedelta(days=10)

#: ``(session, gross return from the previous session)``, oldest first.
Daily = list[tuple[str, float]]

#: yfinance revision whose ``rows`` chunks cover the most history per symbol.
_REVISION = """
SELECT v.revision_id FROM historical_price_revisions AS r
JOIN historical_price_v2_revisions AS v ON v.data_revision = r.data_revision
WHERE r.provider = 'yfinance' AND r.requested_symbol = ?
ORDER BY r.start_date, r.end_date DESC, v.revision_id DESC LIMIT 1
"""
_CHUNKS = """
SELECT c.compressed_payload FROM historical_price_v2_revision_chunks AS m
JOIN historical_price_v2_chunks AS c ON c.chunk_digest = m.chunk_digest
WHERE m.revision_id = ? AND m.chunk_kind = 'rows'
  AND m.chunk_year BETWEEN ? AND ? ORDER BY m.chunk_year
"""


class Series(BaseModel):
    """One ticker's daily gross returns and last close."""

    model_config = ConfigDict(frozen=True)

    daily: Daily
    last_close: float | None


class MonthResult(BaseModel):
    """One portfolio month: members, how many were priced, gross return."""

    model_config = ConfigDict(frozen=True)

    month: str
    members: int
    priced: int
    gross: float


class Summary(BaseModel):
    """Headline numbers of one portfolio over the measured months."""

    model_config = ConfigDict(frozen=True)

    cagr: float
    total: float
    max_drawdown: float
    drawdown_2000_2002: float
    drawdown_2007_2009: float
    coverage: float


def yf_series(rows: Iterable[tuple[str, float | None, float]]) -> Series:
    """Gross returns from yfinance ``(session, close, dividend)`` rows, whose
    closes and dividends are split-adjusted."""
    daily: Daily = []
    previous: float | None = None
    for session, close, dividend in rows:
        if close is None:
            continue
        if previous:
            daily.append((session, (close + dividend) / previous))
        previous = close
    return Series(daily=daily, last_close=previous)


def wiki_series(
    rows: Iterable[tuple[str, float | None, float | None, float | None]],
) -> Series:
    """Gross returns from WIKI ``(date, close, ex_dividend, split_ratio)``
    rows, whose closes are as traded: a split on a date multiplies that day's
    close (and dividend, per new share) by its ratio."""
    daily: Daily = []
    previous: float | None = None
    for day, close, dividend, ratio in rows:
        if close is None:
            continue
        if previous:
            gross = (close + (dividend or 0.0)) * (ratio or 1.0) / previous
            daily.append((day, gross))
        previous = close
    return Series(daily=daily, last_close=previous)


def monthly(series: Series) -> dict[str, float]:
    """Return the gross return of every month the ticker can be held through:
    it has a session in the previous month and in the month."""
    by_month: dict[str, float] = {}
    for session, gross in series.daily:
        by_month[session[:7]] = by_month.get(session[:7], 1.0) * gross
    sessions = {s[:7] for s, _ in series.daily}
    return {m: g for m, g in by_month.items() if previous_month(m) in sessions}


def with_terminal_price(
    returns: dict[str, float],
    series: Series,
    exit_date: str,
    terminal_price: float | None,
) -> dict[str, float]:
    """Settle the last month at ``terminal_price`` instead of the last close
    when prices stop within ``DELISTED_WITHIN`` of the removal."""
    if terminal_price is None or not series.daily or not series.last_close:
        return returns
    last_session = series.daily[-1][0]
    gap = abs(date.fromisoformat(last_session) - date.fromisoformat(exit_date))
    if gap > DELISTED_WITHIN or last_session[:7] not in returns:
        return returns
    month = last_session[:7]
    adjusted = returns[month] * terminal_price / series.last_close
    return {**returns, month: adjusted}


def portfolio(
    months: list[str],
    universe: dict[str, list[str]],
    returns: dict[str, dict[str, float]],
) -> list[MonthResult]:
    """Equal-weight each month's priced members of ``universe[month]``; a
    month with none priced earns nothing (gross 1)."""
    results = []
    for month in months:
        members = universe.get(month, [])
        grosses = [returns[t][month] for t in members if month in returns.get(t, {})]
        gross = sum(grosses) / len(grosses) if grosses else 1.0
        results.append(
            MonthResult(
                month=month, members=len(members), priced=len(grosses), gross=gross
            )
        )
    return results


def summarise(results: list[MonthResult]) -> Summary:
    """CAGR, total return, drawdowns and average coverage of ``results``."""
    values, value = [], 1.0
    for r in results:
        value *= r.gross
        values.append((r.month, value))
    years = len(results) / 12
    members = sum(r.members for r in results)
    return Summary(
        cagr=value ** (1 / years) - 1 if years else 0.0,
        total=value - 1,
        max_drawdown=_drawdown(values),
        drawdown_2000_2002=_drawdown([v for v in values if "2000" <= v[0] < "2003"]),
        drawdown_2007_2009=_drawdown([v for v in values if "2007" <= v[0] < "2010"]),
        coverage=sum(r.priced for r in results) / members if members else 0.0,
    )


def _drawdown(values: list[tuple[str, float]]) -> float:
    """Largest peak-to-trough fall (a negative share) within ``values``."""
    peak, worst = 0.0, 0.0
    for _, value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def previous_month(month: str) -> str:
    """Return the ``YYYY-MM`` before ``month``."""
    first = date.fromisoformat(f"{month}-01") - timedelta(days=1)
    return first.isoformat()[:7]


def load_yf(price_db: Path, symbol: str, first_year: int, last_year: int) -> Series:
    """Read ``symbol``'s yfinance rows for the years (read-only)."""
    conn = read_only(price_db)
    try:
        row = conn.execute(_REVISION, (symbol,)).fetchone()
        if row is None:
            return Series(daily=[], last_close=None)
        payloads = conn.execute(_CHUNKS, (row[0], first_year, last_year)).fetchall()
    finally:
        conn.close()
    return yf_series(_yf_rows(p[0] for p in payloads))


def _yf_rows(payloads: Iterable[bytes]) -> Iterable[tuple[str, float | None, float]]:
    for payload in payloads:
        for item in json.loads(zlib.decompress(payload))["items"]:
            close = item.get("close")
            dividend = item.get("dividends")
            yield (
                item["session"],
                None if close is None else float.fromhex(close),
                0.0 if dividend is None else float.fromhex(dividend),
            )


def load_wiki(wiki_db: Path, ticker: str, start: str, end: str) -> Series:
    """Read ``ticker``'s WIKI rows between the dates (read-only)."""
    conn: sqlite3.Connection = read_only(wiki_db)
    try:
        rows = conn.execute(
            "SELECT date, close, ex_dividend, split_ratio FROM wiki_prices"
            " WHERE ticker = ? AND date BETWEEN ? AND ? ORDER BY date",
            (ticker, start, end),
        ).fetchall()
    finally:
        conn.close()
    return wiki_series(rows)
