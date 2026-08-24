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
from dataclasses import dataclass
from datetime import datetime, timezone

from app.schemas.source_health import SourceName, SourceResult, SourceState

_MIN_CALL_INTERVAL = 2.0  # seconds between calls (polite rate limit)
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
_UK_MIN_MARKET_CAP = 200_000_000  # ~£160M at current rates
_UK_MIN_AVG_VOL = 100_000
_UK_MIN_PRICE = 100.0  # 100p = £1 minimum

# TradingView's `sector` column uses a TRBC economic-sector taxonomy that
# differs from the yfinance/GICS labels the rest of the app uses (US records,
# SECTOR_TO_AV_TOPIC). Map it so UK sectors bucket alongside US ones. Values
# with no clean GICS equivalent map to None and fall through to Unknown.
# REITs surface under TRBC "Finance", so they land in Financial Services.
_TRBC_TO_GICS: dict[str, str] = {
    "Technology Services": "Technology",
    "Electronic Technology": "Technology",
    "Finance": "Financial Services",
    "Health Technology": "Healthcare",
    "Health Services": "Healthcare",
    "Retail Trade": "Consumer Cyclical",
    "Consumer Durables": "Consumer Cyclical",
    "Consumer Services": "Consumer Cyclical",
    "Consumer Non-Durables": "Consumer Defensive",
    "Energy Minerals": "Energy",
    "Non-Energy Minerals": "Basic Materials",
    "Process Industries": "Basic Materials",
    "Producer Manufacturing": "Industrials",
    "Industrial Services": "Industrials",
    "Commercial Services": "Industrials",
    "Distribution Services": "Industrials",
    "Transportation": "Industrials",
    "Communications": "Communication Services",
    "Utilities": "Utilities",
}


def map_sector(raw_sector: object) -> str | None:
    """Map a TradingView TRBC sector string to the app's GICS label, or None.

    Unknown, empty, or non-string values (e.g. NaN) return None so the caller
    falls back to its existing sector resolution rather than storing a label
    the rest of the app doesn't recognise.
    """
    if not isinstance(raw_sector, str):
        return None
    return _TRBC_TO_GICS.get(raw_sector.strip())


def _extract_roster_rows(df, *, is_uk: bool) -> tuple[dict[str, str], ...]:
    """Retain source exchange/currency evidence before compatibility stripping."""
    required = {"name", "exchange", "currency"}
    if not required.issubset(df.columns):
        return ()
    rows: list[dict[str, str]] = []
    symbols = df["ticker"] if "ticker" in df.columns else df["name"]
    for name, exchange, currency in zip(
        symbols, df["exchange"], df["currency"], strict=True
    ):
        if not all(
            isinstance(value, str) and value.strip()
            for value in (name, exchange, currency)
        ):
            return ()
        expected = "LSE" if is_uk else exchange.strip().upper()
        if (is_uk and exchange.strip().upper() != expected) or (
            not is_uk and expected not in _US_EXCHANGES
        ):
            return ()
        if is_uk and currency.strip() not in {"GBP", "GBX", "GBp"}:
            return ()
        source_symbol = name.strip()
        if ":" not in source_symbol:
            source_symbol = f"{expected}:{source_symbol}"
        rows.append(
            {
                "symbol": source_symbol,
                "exchange": exchange.strip().upper(),
                "currency": (
                    "GBP"
                    if is_uk and currency.strip() in {"GBP", "GBX", "GBp"}
                    else currency.strip()
                ),
                "quote_unit": "GBp" if is_uk else currency.strip(),
            }
        )
    return tuple(rows)


@dataclass(frozen=True)
class TradingViewRosterEvidence:
    result: SourceResult
    rows: tuple[dict[str, str], ...]


def ScreenerResult(  # noqa: N802
    tickers: list[str], status: str, detail: str = ""
) -> SourceResult:
    """Backward-compatible constructor for callers of the former local type."""
    if status in {"ok", "empty"}:
        return SourceResult.from_items(
            SourceName.TRADINGVIEW_US, tickers, display_message=detail
        )
    return SourceResult.unavailable(
        SourceName.TRADINGVIEW_US,
        SourceState(status),
        "provider_failure",
        detail,
    )


def _fetch_evidence(
    exchanges: list[str],
    min_market_cap: int,
    min_avg_vol: int,
    min_price: float,
    max_rows: int,
    label: str,
) -> TradingViewRosterEvidence:
    """Core screener fetch — shared by US and UK callers."""
    started_at = datetime.now(timezone.utc)
    source = (
        SourceName.TRADINGVIEW_UK
        if exchanges == _UK_EXCHANGES
        else SourceName.TRADINGVIEW_US
    )
    try:
        from tradingview_screener import Query, col  # type: ignore[import]
    except ImportError:
        print(f"  [skip] {label}: tradingview-screener not installed")
        return TradingViewRosterEvidence(
            SourceResult.unavailable(
                source,
                SourceState.SKIPPED,
                "dependency_missing",
                "TradingView screener dependency is not installed.",
                started_at=started_at,
            ),
            (),
        )

    is_uk = exchanges == _UK_EXCHANGES
    try:
        time.sleep(_MIN_CALL_INTERVAL)
        query = Query()
        # LSE lives in TradingView's "uk" market region; the default query
        # only scans US listings, so an LSE filter would match nothing.
        if is_uk:
            query = query.set_markets("uk")
        conditions = [
            col("exchange").isin(exchanges),
            col("type") == "stock",
            col("market_cap_basic") > min_market_cap,
            col("average_volume_30d_calc") > min_avg_vol,
            col("close") > min_price,
            col("close") > col("SMA200"),
            col("SMA50") > col("SMA150"),
        ]
        if is_uk:
            conditions.append(col("currency").isin(["GBP", "GBX", "GBp"]))
        count, df = (
            query.select(
                "name",
                "exchange",
                "currency",
                "close",
                "SMA50",
                "SMA150",
                "SMA200",
                "average_volume_30d_calc",
                "price_52_week_high",
                "market_cap_basic",
                "sector",
            )
            .where(*conditions)
            .order_by("market_cap_basic", ascending=False)
            .limit(max_rows)
            .get_scanner_data()
        )
    except Exception as exc:
        print(f"  [warn] {label}: screener call failed -- {exc}")
        return TradingViewRosterEvidence(
            SourceResult.unavailable(
                source,
                SourceState.FAILED,
                "provider_failure",
                f"{label} request failed.",
                started_at=started_at,
            ),
            (),
        )

    mask = df["close"] >= df["price_52_week_high"] * _PCT_FROM_HIGH_THRESHOLD
    filtered = df[mask]

    # Strip exchange prefix: "NASDAQ:AAPL" -> "AAPL", "LSE:ULVR" -> "ULVR"
    tickers = [str(name).split(":")[-1] for name in filtered["name"]]

    # UK sectors are unreliable via yfinance/Alpha Vantage (US-only), so carry
    # the TradingView-provided sector (mapped to GICS) for the scanner to use
    # as a fallback. US records already get a reliable yfinance sector.
    sectors: dict[str, str] = {}
    if is_uk and "sector" in filtered.columns:
        for name, raw_sector in zip(filtered["name"], filtered["sector"]):
            ticker = str(name).split(":")[-1]
            mapped = map_sector(raw_sector)
            if mapped:
                sectors[ticker] = mapped

    print(
        f"  [ok] {label}: {len(tickers)} tickers "
        f"(from {count} server-side, {len(df)} fetched, "
        f"{len(filtered)} after 52w-high filter)"
    )
    result = SourceResult.from_items(
        source, tickers, started_at=started_at, sectors=sectors
    )
    return TradingViewRosterEvidence(
        result, _extract_roster_rows(filtered, is_uk=is_uk)
    )


def _fetch(
    exchanges: list[str],
    min_market_cap: int,
    min_avg_vol: int,
    min_price: float,
    max_rows: int,
    label: str,
) -> SourceResult:
    return _fetch_evidence(
        exchanges, min_market_cap, min_avg_vol, min_price, max_rows, label
    ).result


def fetch_tv_screener_tickers(max_rows: int = _US_MAX_ROWS) -> list[str]:
    """Return US tickers (NYSE/NASDAQ) matching the Minervini pre-filter."""
    return fetch_tv_screener_result(max_rows).tickers


def fetch_tv_screener_result(max_rows: int = _US_MAX_ROWS) -> SourceResult:
    """Return the US screen output together with a machine-readable status."""
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
    return fetch_tv_screener_result_uk(max_rows).tickers


def fetch_tv_screener_result_uk(max_rows: int = _UK_MAX_ROWS) -> SourceResult:
    """Return the UK screen output together with a machine-readable status."""
    return _fetch(
        exchanges=_UK_EXCHANGES,
        min_market_cap=_UK_MIN_MARKET_CAP,
        min_avg_vol=_UK_MIN_AVG_VOL,
        min_price=_UK_MIN_PRICE,
        max_rows=max_rows,
        label="tv_screener_uk",
    )


def fetch_tv_screener_roster_evidence(
    *, market: str, max_rows: int | None = None
) -> TradingViewRosterEvidence:
    """Return current screener output with source exchange/currency evidence."""
    if market.upper() == "UK":
        return _fetch_evidence(
            _UK_EXCHANGES,
            _UK_MIN_MARKET_CAP,
            _UK_MIN_AVG_VOL,
            _UK_MIN_PRICE,
            max_rows or _UK_MAX_ROWS,
            "tv_screener_uk",
        )
    if market.upper() == "US":
        return _fetch_evidence(
            _US_EXCHANGES,
            _US_MIN_MARKET_CAP,
            _US_MIN_AVG_VOL,
            _US_MIN_PRICE,
            max_rows or _US_MAX_ROWS,
            "tv_screener_us",
        )
    raise ValueError("market must be US or UK")


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
