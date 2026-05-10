"""Congressional trading client — scrapes QuiverQuant per-ticker pages.

Counts Buy/Sell transactions by Congress members (both chambers) and Senate-only
in the last 12 months.  No API key required; polite rate limiting.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

_BASE_URL = "https://www.quiverquant.com/congresstrading/stock/{ticker}"
_MIN_CALL_INTERVAL = 3.0  # seconds between requests
_LOOKBACK_DAYS = 365
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


@dataclass
class CongressStats:
    """Congressional trading stats for a single ticker over the lookback window."""

    buys: int = 0
    sells: int = 0
    senate_buys: int = 0
    senate_sells: int = 0


class CongressClient:
    """Rate-limited scraper for congressional trading data from QuiverQuant."""

    def __init__(self) -> None:
        self._cache: dict[str, CongressStats | None] = {}
        self._last_call: float = 0.0
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    def _fetch_html(self, ticker: str) -> str | None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)
        url = _BASE_URL.format(ticker=ticker)
        try:
            resp = self._session.get(url, timeout=20)
            self._last_call = time.monotonic()
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            print(f"[congress] {ticker}: request error — {exc}")
            return None

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
        """Return congressional trading stats for the last 12 months."""
        if ticker in self._cache:
            return self._cache[ticker]
        html = self._fetch_html(ticker)
        result = self._parse_stats(html) if html else None
        self._cache[ticker] = result
        return result

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
