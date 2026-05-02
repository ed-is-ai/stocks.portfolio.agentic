"""TradingView screener extractor — fetches a pre-filtered stock universe.

Uses the unofficial TradingView scanner endpoint (no API key required).
Supports US (NYSE/NASDAQ) and UK (LSE) markets.

US filters (hard, server-side):
  - NYSE / NASDAQ only
  - Stock type only (excludes ETFs, ADRs, warrants)
  - Market cap  > $500M
  - Avg 30d vol > 300k shares
  - Price       > $10
  - Price       > SMA200  (Stage 2 minimum)
  - SMA50       > SMA150  (partial Minervini alignment)

UK filters (hard, server-side):
  - LSE only
  - Stock type only
  - Market cap  > $200M  (USD equivalent, ~£160M)
  - Avg 30d vol > 100k shares
  - Price       > 100     (pence — LSE prices quoted in GBp)
  - Price       > SMA200
  - SMA50       > SMA150

Soft filter (both markets):
  - Price within 35% of 52w high
"""

from __future__ import annotations

import time

_MIN_CALL_INTERVAL = 2.0       # seconds between calls (polite rate limit)
_PCT_FROM_HIGH_THRESHOLD = 0.65  # close must be >= 65% of 52w high

# US defaults
_US_MAX_ROWS = 500
_US_EXCHANGES = ["NASDAQ", "NYSE"]
_US_MIN_MARKET_CAP = 500_000_000
_US_MIN_AVG_VOL = 300_000
_US_MIN_PRICE = 10.0

# UK defaults — LSE prices are in pence (GBp), market cap field is USD on TV
_UK_MAX_ROWS = 200
_UK_EXCHANGES = ["LSE"]
_UK_MIN_MARKET_CAP = 200_000_000   # ~£160M at current rates
_UK_MIN_AVG_VOL = 100_000
_UK_MIN_PRICE = 100.0              # 100p = £1 minimum


def _fetch(
    exchanges: list[str],
    min_market_cap: int,
    min_avg_vol: int,
    min_price: float,
    max_rows: int,
    label: str,
) -> list[str]:
    """Core screener fetch — shared by US and UK callers."""
    try:
        from tradingview_screener import Query, col  # type: ignore[import]
    except ImportError:
        print(f"  [skip] {label}: tradingview-screener not installed")
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
                col("exchange").isin(exchanges),
                col("type") == "stock",
                col("market_cap_basic") > min_market_cap,
                col("average_volume_30d_calc") > min_avg_vol,
                col("close") > min_price,
                col("close") > col("SMA200"),
                col("SMA50") > col("SMA150"),
            )
            .order_by("market_cap_basic", ascending=False)
            .limit(max_rows)
            .get_scanner_data()
        )
    except Exception as exc:
        print(f"  [warn] {label}: screener call failed -- {exc}")
        return []

    mask = df["close"] >= df["price_52_week_high"] * _PCT_FROM_HIGH_THRESHOLD
    filtered = df[mask]

    # Strip exchange prefix: "NASDAQ:AAPL" -> "AAPL", "LSE:ULVR" -> "ULVR"
    tickers = [str(name).split(":")[-1] for name in filtered["name"]]

    print(
        f"  [ok] {label}: {len(tickers)} tickers "
        f"(from {count} server-side, {len(df)} fetched, "
        f"{len(filtered)} after 52w-high filter)"
    )
    return tickers


def fetch_tv_screener_tickers(max_rows: int = _US_MAX_ROWS) -> list[str]:
    """Return US tickers (NYSE/NASDAQ) matching the Minervini pre-filter."""
    return _fetch(
        exchanges=_US_EXCHANGES,
        min_market_cap=_US_MIN_MARKET_CAP,
        min_avg_vol=_US_MIN_AVG_VOL,
        min_price=_US_MIN_PRICE,
        max_rows=max_rows,
        label="tv_screener_us",
    )


def fetch_tv_screener_tickers_uk(max_rows: int = _UK_MAX_ROWS) -> list[str]:
    """Return UK LSE tickers matching the Minervini pre-filter.

    Prices on LSE are quoted in pence (GBp) so the min_price threshold
    is 100 (= £1). Market cap is still USD-denominated in TradingView.
    Tickers are returned without the LSE: prefix (e.g. "ULVR", "AZN").
    Note: yfinance requires ".L" suffix for LSE tickers (e.g. "ULVR.L");
    the scanner appends this automatically.
    """
    return _fetch(
        exchanges=_UK_EXCHANGES,
        min_market_cap=_UK_MIN_MARKET_CAP,
        min_avg_vol=_UK_MIN_AVG_VOL,
        min_price=_UK_MIN_PRICE,
        max_rows=max_rows,
        label="tv_screener_uk",
    )


if __name__ == "__main__":
    import sys
    market = sys.argv[1].upper() if len(sys.argv) > 1 else "US"
    if market == "UK":
        tickers = fetch_tv_screener_tickers_uk()
        label = "UK (LSE)"
    else:
        tickers = fetch_tv_screener_tickers()
        label = "US (NYSE/NASDAQ)"
    print(f"\n{label} total: {len(tickers)}")
    for t in tickers[:20]:
        print(f"  {t}")
    if len(tickers) > 20:
        print(f"  ... and {len(tickers) - 20} more")
