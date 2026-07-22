"""Congressional trading client — scrapes QuiverQuant per-ticker pages.

Counts Buy/Sell transactions by Congress members (both chambers) and Senate-only
in the last 12 months.  No API key required; polite rate limiting.

Results are cached persistently (SQLite, ``CONGRESS_CACHE_DB``) for
``_CACHE_TTL_SECONDS`` — congressional filings don't change intraday, so a
ticker fetched earlier today is served from cache on every later run. Cache
misses are fetched across ``_PARALLEL_LANES`` independent sessions, each
individually paced at ``_MIN_CALL_INTERVAL``, so aggregate throughput to
QuiverQuant is ``_PARALLEL_LANES``x a single polite scraper rather than
strictly serial. This trades some scraping politeness for scan speed on a
large, mostly-uncached ticker universe — reduce ``_PARALLEL_LANES`` back to 1
if QuiverQuant starts rate-limiting or blocking requests.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from app.core.config import CONGRESS_CACHE_DB

_BASE_URL = "https://www.quiverquant.com/congresstrading/stock/{ticker}"
_MIN_CALL_INTERVAL = 3.0  # seconds between requests, per lane
_PARALLEL_LANES = 3  # concurrent scrape lanes for cache misses
_LOOKBACK_DAYS = 365
_CACHE_TTL_SECONDS = 24 * 60 * 60  # congressional filings don't change intraday
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_CHAMBER_RE = re.compile(r"congresstrading/trade/(Senate|House)-")
_TYPE_RE = re.compile(r"<span[^>]*>\s*(Purchase|Sale|Exchange)\b")
_DATE_RE = re.compile(r"([A-Z][a-z]+ \d+, \d{4})")

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS congress_cache (
    ticker       TEXT PRIMARY KEY,
    has_data     INTEGER NOT NULL,
    buys         INTEGER NOT NULL DEFAULT 0,
    sells        INTEGER NOT NULL DEFAULT 0,
    senate_buys  INTEGER NOT NULL DEFAULT 0,
    senate_sells INTEGER NOT NULL DEFAULT 0,
    fetched_at   TEXT NOT NULL
);
"""


@dataclass
class CongressStats:
    """Congressional trading stats for a single ticker over the lookback window."""

    buys: int = 0
    sells: int = 0
    senate_buys: int = 0
    senate_sells: int = 0


class _Lane:
    """An independent, self-paced HTTP session for one scrape worker."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._last_call: float = 0.0

    def fetch_html(self, ticker: str) -> tuple[str | None, bool]:
        """Return ``(html, request_failed)``.

        ``request_failed`` distinguishes a genuine "no filings" response
        (HTTP 404 — ``request_failed=False``) from a real network/HTTP
        failure (timeout, connection error, 5xx — ``request_failed=True``).
        Both return ``html=None``, but only the former is safe to report as
        "no data available"; the latter must surface as a failed source
        rather than being silently folded into "empty".
        """
        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)
        url = _BASE_URL.format(ticker=ticker)
        try:
            resp = self._session.get(url, timeout=20)
            self._last_call = time.monotonic()
            if resp.status_code == 404:
                return None, False
            resp.raise_for_status()
            return resp.text, False
        except Exception as exc:
            print(f"[congress] {ticker}: request error — {exc}")
            return None, True


class CongressClient:
    """Rate-limited, persistently-cached scraper for QuiverQuant congressional data."""

    def __init__(self, cache_db_path: str | Path | None = None) -> None:
        self._cache_db_path = str(cache_db_path or CONGRESS_CACHE_DB)
        self._schema_ready = False
        self._default_lane = _Lane()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._cache_db_path)
        if not self._schema_ready:
            conn.executescript(_CACHE_SCHEMA)
            conn.commit()
            self._schema_ready = True
        return conn

    def _cache_get(self, ticker: str) -> tuple[bool, CongressStats | None]:
        """Return (hit, stats). hit is False on a miss or a stale entry."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT has_data, buys, sells, senate_buys, senate_sells, "
                "fetched_at FROM congress_cache WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return False, None
        has_data, buys, sells, senate_buys, senate_sells, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        if age > _CACHE_TTL_SECONDS:
            return False, None
        if not has_data:
            return True, None
        return True, CongressStats(buys, sells, senate_buys, senate_sells)

    def _cache_put(self, ticker: str, stats: CongressStats | None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO congress_cache "
                "(ticker, has_data, buys, sells, senate_buys, senate_sells, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "has_data=excluded.has_data, buys=excluded.buys, "
                "sells=excluded.sells, senate_buys=excluded.senate_buys, "
                "senate_sells=excluded.senate_sells, fetched_at=excluded.fetched_at",
                (
                    ticker,
                    int(stats is not None),
                    stats.buys if stats else 0,
                    stats.sells if stats else 0,
                    stats.senate_buys if stats else 0,
                    stats.senate_sells if stats else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _parse_stats(self, html: str) -> CongressStats:
        """Parse buy/sell counts by chamber from HTML table rows."""
        cutoff = date.today() - timedelta(days=_LOOKBACK_DAYS)
        counts: Counter[str] = Counter()

        for row in html.split("<tr>"):
            m_chamber = _CHAMBER_RE.search(row)
            if not m_chamber:
                continue
            chamber = m_chamber.group(1)

            m_type = _TYPE_RE.search(row)
            if not m_type:
                continue
            txn_type = m_type.group(1).strip()

            # Last date match in row is the Traded date (5th column)
            dates = _DATE_RE.findall(row)
            if not dates:
                continue
            try:
                txn_date = datetime.strptime(dates[-1], "%b %d, %Y").date()
            except ValueError:
                continue

            if txn_date < cutoff:
                continue

            counts[f"{chamber}_{txn_type}"] += 1

        return CongressStats(
            buys=counts["House_Purchase"] + counts["Senate_Purchase"],
            sells=counts["House_Sale"] + counts["Senate_Sale"],
            senate_buys=counts["Senate_Purchase"],
            senate_sells=counts["Senate_Sale"],
        )

    def get_stats(self, ticker: str) -> CongressStats | None:
        """Return congressional trading stats for the last 12 months.

        Serves from the persistent cache when fresh; otherwise fetches via
        the default rate-limited lane and caches the result.
        """
        hit, cached = self._cache_get(ticker)
        if hit:
            return cached
        html, _request_failed = self._default_lane.fetch_html(ticker)
        result = self._parse_stats(html) if html else None
        self._cache_put(ticker, result)
        return result

    def get_stats_many(
        self,
        tickers: list[str],
        *,
        failed_tickers: list[str] | None = None,
    ) -> dict[str, CongressStats | None]:
        """Return stats for many tickers, using the cache and parallel lanes.

        Cache hits are resolved immediately. Cache misses are fetched across
        ``_PARALLEL_LANES`` independent sessions (each individually paced at
        ``_MIN_CALL_INTERVAL``) so a large, mostly-uncached ticker universe
        doesn't pay the full serial cost. Results are returned keyed by
        ticker; the caller is responsible for any output ordering.

        When *failed_tickers* is given, any ticker whose fetch genuinely
        failed (network/HTTP error, not a clean "no filings" 404) is
        appended to it. This lets a caller distinguish "no congressional
        data exists for these tickers" (a real, reportable result) from "the
        request to fetch congressional data failed" (a source outage).
        """
        results: dict[str, CongressStats | None] = {}
        misses: list[str] = []
        for ticker in tickers:
            hit, cached = self._cache_get(ticker)
            if hit:
                results[ticker] = cached
            else:
                misses.append(ticker)

        if not misses:
            return results

        lanes = [_Lane() for _ in range(min(_PARALLEL_LANES, len(misses)))]

        def _fetch_one(
            index_and_ticker: tuple[int, str],
        ) -> tuple[str, CongressStats | None, bool]:
            index, ticker = index_and_ticker
            lane = lanes[index % len(lanes)]
            html, request_failed = lane.fetch_html(ticker)
            stats = self._parse_stats(html) if html else None
            return ticker, stats, request_failed

        with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            for ticker, stats, request_failed in pool.map(
                _fetch_one, enumerate(misses)
            ):
                results[ticker] = stats
                self._cache_put(ticker, stats)
                if request_failed and failed_tickers is not None:
                    failed_tickers.append(ticker)

        return results

    def get_congress_buys(self, ticker: str) -> int | None:
        """Return number of congressional buy transactions in the last 12 months."""
        stats = self.get_stats(ticker)
        return stats.buys if stats else None


if __name__ == "__main__":
    import sys

    tickers = sys.argv[1:] or ["AAPL", "NVDA", "MSFT"]
    client = CongressClient()
    for t in tickers:
        s = client.get_stats(t)
        if s:
            print(
                f"{t}: buys={s.buys} sells={s.sells} "
                f"senate_buys={s.senate_buys} senate_sells={s.senate_sells}"
            )
        else:
            print(f"{t}: no data")
