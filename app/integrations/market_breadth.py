"""S&P 500 market-breadth client — keyless public CSV feed (#109).

Fetches the latest "% of S&P 500 above 200DMA" reading from TraderMonty's
public market-breadth CSV (the same source the market-top-detector skill
uses). No API key required. Feeds the narrative layer a genuine market-wide
participation signal, which the scan's filtered candidate list cannot provide.

Mirrors the GDELT client's resilience: the fetch is retried with exponential
backoff via tenacity, and a fully exhausted retry (or any parse failure)
returns ``None`` rather than raising, so the narrative degrades gracefully to
"no breadth data" instead of failing the pipeline.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.schemas.market_breadth import MarketBreadth

_BREADTH_CSV_URL = (
    "https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv"
)
_TIMEOUT_SECONDS = 20
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2.0
_FRESH_MAX_DAYS = 7


@retry(
    retry=retry_if_exception_type(Exception),
    wait=wait_exponential(multiplier=_RETRY_BACKOFF_SECONDS),
    stop=stop_after_attempt(_RETRY_ATTEMPTS),
    retry_error_callback=lambda _state: "",
)
def _fetch_csv(url: str) -> str:
    """Fetch the raw breadth CSV text, or ``""`` after exhausted retries."""
    response = requests.get(url, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def _to_pct(raw: str) -> float:
    """Convert a 0..1 breadth-index string to a 0..100 percentage."""
    return round(float(raw) * 100, 2)


def _parse_latest(text: str) -> MarketBreadth | None:
    """Parse the newest row of the breadth CSV into a ``MarketBreadth``."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return None
    latest = max(rows, key=lambda r: r.get("Date", ""))
    date_str = latest["Date"].strip()
    reading_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    today = datetime.now(timezone.utc).date()
    days_old = (today - reading_date).days if reading_date <= today else None
    smoothed = latest.get("Breadth_Index_8MA")
    trend = latest.get("Breadth_200MA_Trend")
    return MarketBreadth(
        pct_above_200dma=_to_pct(latest["Breadth_Index_Raw"]),
        smoothed_8ma=_to_pct(smoothed) if smoothed else None,
        trend_rising=(int(trend) > 0) if trend not in (None, "") else None,
        bearish_signal=str(latest.get("Bearish_Signal", "")).strip().lower()
        in ("true", "1", "yes"),
        as_of=date_str,
        is_fresh=(days_old is not None and days_old <= _FRESH_MAX_DAYS),
        days_old=days_old,
    )


def fetch_market_breadth(url: str = _BREADTH_CSV_URL) -> MarketBreadth | None:
    """Return the latest S&P 500 breadth reading, or ``None`` on any failure.

    Never raises: a network/timeout failure (after retries), an empty feed, or
    an unparseable row all yield ``None`` so the caller can treat breadth as
    best-effort context.
    """
    text = _fetch_csv(url)
    if not text:
        return None
    try:
        return _parse_latest(text)
    except (KeyError, ValueError, TypeError):
        return None
