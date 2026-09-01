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
import math
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
_LOOKBACK_ROWS = 5  # ~one trading week (feed publishes on trading days only)
_NEAR_TERM_MAX_SPAN_DAYS = 21  # beyond this, "past week" deltas would be a lie

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
    rows.sort(key=lambda r: r.get("Date") or "")
    # A duplicate latest Date resolves to the last (most recently appended =
    # restated) row, since a stable sort preserves original order for ties.
    latest = rows[-1]
    prior_idx = len(rows) - 1 - _LOOKBACK_ROWS
    prior = rows[prior_idx] if prior_idx >= 0 else None
    date_str = latest["Date"].strip()
    reading_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    today = _utc_today()
    days_old = (today - reading_date).days if reading_date <= today else None
    smoothed = latest.get("Breadth_Index_8MA")
    trend = latest.get("Breadth_200MA_Trend")
    latest_pct = _to_pct(latest["Breadth_Index_Raw"])

    # A stale/gappy prior row (feed outage, holiday cluster) or a negative gap
    # makes the "past week" deltas meaningless — drop them entirely.
    if prior is not None:
        try:
            prior_date = datetime.strptime(
                (prior.get("Date") or "").strip(), "%Y-%m-%d"
            ).date()
            span = (reading_date - prior_date).days
            if span < 0 or span > _NEAR_TERM_MAX_SPAN_DAYS:
                prior = None
        except (ValueError, TypeError):
            prior = None

    near_term_pct_delta: float | None = None
    try:
        if prior is not None:
            near_term_pct_delta = round(
                latest_pct - _to_pct(prior["Breadth_Index_Raw"]), 2
            )
    except (KeyError, ValueError, TypeError):
        near_term_pct_delta = None

    pct_50dma: float | None = None
    near_term_50dma_pct_delta: float | None = None
    near_term_bearish_signal = False
    try:
        raw_50 = latest.get("Breadth_50_Index_Raw")
        if raw_50 not in (None, ""):
            pct_50dma = _to_pct(raw_50)
            if prior is not None:
                prior_50 = prior.get("Breadth_50_Index_Raw")
                if prior_50 not in (None, ""):
                    near_term_50dma_pct_delta = round(pct_50dma - _to_pct(prior_50), 2)
        near_term_bearish_signal = str(
            latest.get("Bearish_Signal_50", "")
        ).strip().lower() in ("true", "1", "yes")
    except (KeyError, ValueError, TypeError):
        pct_50dma = None
        near_term_50dma_pct_delta = None
        near_term_bearish_signal = False

    # Feed can emit literal "nan"/"inf" which float() accepts; a non-finite
    # value here would later abort narrative_input_digest and force an LLM
    # regen every run. Null the offending field instead.
    if near_term_pct_delta is not None and not math.isfinite(near_term_pct_delta):
        near_term_pct_delta = None
    if pct_50dma is not None and not math.isfinite(pct_50dma):
        pct_50dma = None
        near_term_50dma_pct_delta = None
    if near_term_50dma_pct_delta is not None and not math.isfinite(
        near_term_50dma_pct_delta
    ):
        near_term_50dma_pct_delta = None

    return MarketBreadth(
        pct_above_200dma=latest_pct,
        smoothed_8ma=_to_pct(smoothed) if smoothed else None,
        trend_rising=(int(trend) > 0) if trend not in (None, "") else None,
        bearish_signal=str(latest.get("Bearish_Signal", "")).strip().lower()
        in ("true", "1", "yes"),
        near_term_pct_delta=near_term_pct_delta,
        pct_50dma=pct_50dma,
        near_term_50dma_pct_delta=near_term_50dma_pct_delta,
        near_term_bearish_signal=near_term_bearish_signal,
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
