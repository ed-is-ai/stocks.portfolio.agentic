"""TradingView screener extractor — fetches a pre-filtered stock universe.

Uses the unofficial TradingView scanner endpoint (no API key required).
Returns US-listed stocks that satisfy a Minervini Stage 2 pre-filter, giving
the scanner a broad universe of technically constructive names beyond the
WhalWisdom institutional feed.

Filters applied:
  Hard (server-side):
    - NYSE / NASDAQ only
    - Stock type only (excludes ETFs, ADRs, warrants)
    - Market cap  > $500M
    - Avg 30d vol > 300k shares
    - Price       > $10
    - Price       > SMA200   (Stage 2 minimum)
    - SMA50       > SMA150   (partial Minervini alignment)

  Soft (post-fetch):
    - Price within 35% of 52w high (in base or early breakout; cuts deep declines)
"""

from __future__ import annotations

import time
from typing import Any

_MAX_ROWS = 200  # keep pipeline runtime reasonable (~3s/ticker for congress data)
_MIN_CALL_INTERVAL = 2.0  # seconds between calls (polite rate limit)
_PCT_FROM_HIGH_THRESHOLD = 0.65  # close must be >= 65% of 52w high


def fetch_tv_screener_tickers(max_rows: int = _MAX_ROWS) -> list[str]:
    """Return tickers matching the Minervini pre-filter from TradingView.

    Returns an empty list and prints a warning if the screener call fails.
    """
    try:
        from tradingview_screener import Query, col  # type: ignore[import]
    except ImportError:
        print("  [skip] tv_screener: tradingview-screener not installed")
        return []

    try:
        time.sleep(_MIN_CALL_INTERVAL)
        count, df = (
            Query()
            .select(
                "name",
                "close",
                "SMA50",
                "SMA150",
                "SMA200",
                "average_volume_30d_calc",
                "price_52_week_high",
                "market_cap_basic",
            )
            .where(
                col("exchange").isin(["NASDAQ", "NYSE"]),
                col("type") == "stock",
                col("market_cap_basic") > 500_000_000,
                col("average_volume_30d_calc") > 300_000,
                col("close") > 10,
                col("close") > col("SMA200"),
                col("SMA50") > col("SMA150"),
            )
            .order_by("market_cap_basic", ascending=False)
            .limit(max_rows)
            .get_scanner_data()
        )
    except Exception as exc:
        print(f"  [warn] tv_screener: screener call failed — {exc}")
        return []

    # Post-filter: within 35% of 52-week high
    mask = df["close"] >= df["price_52_week_high"] * _PCT_FROM_HIGH_THRESHOLD
    filtered = df[mask]

    # Strip exchange prefix (e.g. "NASDAQ:AAPL" → "AAPL")
    tickers = [
        str(name).split(":")[-1]
        for name in filtered["name"]
    ]

    print(
        f"  [ok] tv_screener: {len(tickers)} tickers "
        f"(from {count} server-side matches, {len(df)} fetched, "
        f"{len(filtered)} after 52w-high filter)"
    )
    return tickers


if __name__ == "__main__":
    tickers = fetch_tv_screener_tickers()
    print(f"\nTotal: {len(tickers)}")
    for t in tickers[:20]:
        print(f"  {t}")
    if len(tickers) > 20:
        print(f"  ... and {len(tickers) - 20} more")
