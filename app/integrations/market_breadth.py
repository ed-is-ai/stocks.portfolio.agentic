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
from datetime import date, datetime, timezone
from threading import RLock

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

# The feed publishes daily, so a process-local cache avoids repeat downloads
# during one UTC day.  It intentionally has no persistence/lifecycle beyond
# this process.  Entries are keyed by URL to keep the injectable test/source
# URL argument from accidentally sharing the production entry.
_daily_cache: dict[str, tuple[date, MarketBreadth]] = {}
_daily_cache_lock = RLock()


def _utc_today() -> date:
    """Return today's UTC date (a small clock seam for deterministic tests)."""
    return datetime.now(timezone.utc).date()


def reset_market_breadth_cache() -> None:
    """Clear the process-local cache, primarily for isolated test runs."""
    with _daily_cache_lock:
        _daily_cache.clear()


def _reset_daily_cache() -> None:
    """Backward-compatible private reset seam for cache-focused tests."""
    reset_market_breadth_cache()


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
    today = _utc_today()
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
    today = _utc_today()
    with _daily_cache_lock:
        cached = _daily_cache.get(url)
        if cached is not None and cached[0] == today:
            # Never return the stored model directly: callers may mutate it.
            return cached[1].model_copy(
                deep=True, update={"retrieval_source": "cached"}
            )

        text = _fetch_csv(url)
        if not text:
            return None
        try:
            breadth = _parse_latest(text)
        except (KeyError, ValueError, TypeError):
            return None
        if breadth is None:
            return None

        # Only a fully parsed value earns a cache entry.  A failed refresh
        # therefore cannot overwrite yesterday's valid diagnostic value.
        _daily_cache[url] = (today, breadth.model_copy(deep=True))
        return breadth.model_copy(deep=True, update={"retrieval_source": "fetched"})
