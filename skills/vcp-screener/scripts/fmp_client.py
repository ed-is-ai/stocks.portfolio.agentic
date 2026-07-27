#!/usr/bin/env python3
"""
FMP API Client for VCP Screener

Provides rate-limited access to Financial Modeling Prep API endpoints
for VCP (Volatility Contraction Pattern) screening.

Features:
- Rate limiting (0.3s between requests)
- Automatic retry on 429 errors
- Session caching for duplicate requests
- Batch quote support
- S&P 500 constituents fetching
"""

import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print(
        "ERROR: requests library not found. Install with: pip install requests",
        file=sys.stderr,
    )
    sys.exit(1)


# --- FMP endpoint fallback: stable (new users) -> v3 (legacy users) ---


def _stable_quote_url(base, symbols_str, params):
    """stable/quote?symbol=^GSPC"""
    params["symbol"] = symbols_str
    return base, params


def _v3_quote_url(base, symbols_str, params):
    """api/v3/quote/^GSPC"""
    return f"{base}/{symbols_str}", params


def _stable_hist_url(base, symbols_str, params):
    """stable/historical-price-eod/full?symbol=AAPL&limit=260"""
    params["symbol"] = symbols_str
    if "timeseries" in params:
        params["limit"] = params.pop("timeseries")
    return base, params


def _v3_hist_url(base, symbols_str, params):
    """api/v3/historical-price-full/^GSPC?timeseries=80"""
    return f"{base}/{symbols_str}", params


_FMP_ENDPOINTS = {
    "quote": [
        ("https://financialmodelingprep.com/stable/quote", _stable_quote_url),
        ("https://financialmodelingprep.com/api/v3/quote", _v3_quote_url),
    ],
    "historical": [
        (
            "https://financialmodelingprep.com/stable/historical-price-eod/full",
            _stable_hist_url,
        ),
        (
            "https://financialmodelingprep.com/api/v3/historical-price-full",
            _v3_hist_url,
        ),
    ],
}


class FMPClient:
    """Client for Financial Modeling Prep API with rate limiting and caching"""

    BASE_URL = "https://financialmodelingprep.com/api/v3"
    RATE_LIMIT_DELAY = 0.3  # 300ms between requests

    _ENDPOINT_FAILURE_THRESHOLD = 3  # disable endpoint after N consecutive failures

    # S&P 500 universe source. FMP's constituent endpoints are unavailable on
    # the free plan (stable -> HTTP 402, legacy v3 -> HTTP 403), so the list is
    # sourced from the free, keyless DataHub open dataset.
    _CONSTITUENTS_URL = (
        "https://datahub.io/core/s-and-p-500-companies-financials/_r/-/"
        "data/constituents-financials.csv"
    )

    # Last-known-good S&P 500 constituent list, so one failed fetch doesn't take
    # down the whole run. Lives alongside the script to survive across runs (the
    # scanner invokes us with an ephemeral --output-dir).
    _CONSTITUENTS_CACHE = Path(__file__).parent / ".cache" / "sp500_constituents.json"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("FMP_API_KEY")
        if not self.api_key:
            raise ValueError(
                "FMP API key required. Set FMP_API_KEY environment variable "
                "or pass api_key parameter."
            )
        self.session = requests.Session()
        self.session.headers.update({"apikey": self.api_key})
        self.cache = {}
        self.last_call_time = 0
        self.rate_limit_reached = False
        self.retry_count = 0
        self.max_retries = 1
        self.api_calls_made = 0
        # Most recent request failure detail (status code + body, or exception),
        # recorded even for quiet calls so callers can surface the real cause.
        self.last_error: Optional[str] = None
        # Circuit breaker: track consecutive failures per endpoint URL prefix
        self._endpoint_failures: dict[str, int] = {}
        self._disabled_endpoints: set[str] = set()

    def _rate_limited_get(
        self, url: str, params: Optional[dict] = None, quiet: bool = False
    ) -> Optional[dict]:
        if self.rate_limit_reached:
            return None

        if params is None:
            params = {}

        elapsed = time.time() - self.last_call_time
        if elapsed < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - elapsed)

        try:
            response = self.session.get(url, params=params, timeout=30)
            self.last_call_time = time.time()
            self.api_calls_made += 1

            if response.status_code == 200:
                self.retry_count = 0
                return response.json()
            elif response.status_code == 429:
                self.retry_count += 1
                if self.retry_count <= self.max_retries:
                    print(
                        "WARNING: Rate limit exceeded. Waiting 60 seconds...",
                        file=sys.stderr,
                    )
                    time.sleep(60)
                    return self._rate_limited_get(url, params, quiet=quiet)
                else:
                    self.last_error = "daily API rate limit reached"
                    print("ERROR: Daily API rate limit reached.", file=sys.stderr)
                    self.rate_limit_reached = True
                    return None
            else:
                # Record the real cause even when quiet, so callers that exhaust
                # a fallback chain can still report why every attempt failed.
                self.last_error = f"HTTP {response.status_code} - {response.text[:200]}"
                if not quiet:
                    print(
                        f"ERROR: API request failed: {response.status_code} - {response.text[:200]}",
                        file=sys.stderr,
                    )
                return None
        except requests.exceptions.RequestException as e:
            self.last_error = f"request exception: {e}"
            print(f"ERROR: Request exception: {e}", file=sys.stderr)
            return None

    def _request_with_fallback(self, endpoint_key, symbols_str, extra_params=None):
        """Try stable endpoint first, fall back to v3 for legacy users.

        Returns parsed JSON in v3-compatible shape, or None if all fail.
        Non-last endpoints use quiet=True to suppress expected 403 stderr.
        """
        params = dict(extra_params) if extra_params else {}
        endpoints = _FMP_ENDPOINTS[endpoint_key]
        is_single = "," not in symbols_str

        for i, (base_url, url_builder) in enumerate(endpoints):
            # Circuit breaker: skip endpoints with too many consecutive failures
            if base_url in self._disabled_endpoints:
                continue

            url, final_params = url_builder(base_url, symbols_str, dict(params))
            is_last = i == len(endpoints) - 1
            data = self._rate_limited_get(url, final_params, quiet=not is_last)
            if data is None:  # HTTP error — count against this endpoint
                self._record_endpoint_failure(base_url)
                continue
            if not data:  # empty response (no data for symbol) — skip, don't penalise
                return None

            # Shape validation: reject truthy-but-wrong-shape responses
            valid = True
            if endpoint_key == "quote":
                if not isinstance(data, list) or len(data) == 0:
                    valid = False
                elif is_single and not any(
                    q.get("symbol", "").replace("-", ".")
                    == symbols_str.replace("-", ".")
                    for q in data
                ):
                    valid = False

            if endpoint_key == "historical":
                # stable/historical-price-eod/full returns a flat list — normalise to v3 shape
                if isinstance(data, list) and data:
                    data = {
                        "symbol": data[0].get("symbol", symbols_str),
                        "historical": data,
                    }
                if not isinstance(data, dict):
                    valid = False
                elif "historicalStockList" in data:
                    # stable batch format -> v3 single format (exact match only)
                    norm = symbols_str.replace("-", ".")
                    found = None
                    for entry in data["historicalStockList"]:
                        if entry.get("symbol", "").replace("-", ".") == norm:
                            found = {
                                "symbol": entry.get("symbol"),
                                "historical": entry.get("historical", []),
                            }
                            break
                    if found:
                        self._endpoint_failures[base_url] = 0
                        return found
                    valid = False
                elif "historical" not in data:
                    valid = False
                elif is_single and data.get("symbol"):
                    if data["symbol"].replace("-", ".") != symbols_str.replace(
                        "-", "."
                    ):
                        valid = False

            if valid:
                self._endpoint_failures[base_url] = 0
                return data
            self._record_endpoint_failure(base_url)
        return None

    def _record_endpoint_failure(self, base_url: str) -> None:
        """Track consecutive failures and disable endpoint after threshold."""
        failures = self._endpoint_failures.get(base_url, 0) + 1
        self._endpoint_failures[base_url] = failures
        if failures >= self._ENDPOINT_FAILURE_THRESHOLD:
            self._disabled_endpoints.add(base_url)

    def get_sp500_constituents(self) -> Optional[list[dict]]:
        """Fetch the S&P 500 constituent list from the DataHub open dataset.

        FMP's own constituent endpoints are unavailable on the free plan
        (``stable`` -> HTTP 402, legacy ``v3`` -> HTTP 403), so the universe is
        sourced from the free, keyless DataHub ``s-and-p-500-companies`` CSV.
        On a fresh success the list is cached to disk as last-known-good; if the
        live fetch fails, falls back to that cache (with a staleness warning) so
        a single upstream failure doesn't take down the whole run.

        Returns:
            List of dicts with keys: symbol, name, sector, subSector
            or None if neither a live fetch nor a cached fallback is available.
        """
        cache_key = "sp500_constituents"
        if cache_key in self.cache:
            return self.cache[cache_key]

        self.last_error = None
        constituents = self._fetch_datahub_constituents()
        if constituents:
            self.cache[cache_key] = constituents
            self._save_constituents_cache(constituents)
            return constituents

        reason = self.last_error or "no data returned from DataHub"
        print(
            f"ERROR: Unable to fetch S&P 500 constituents ({reason})",
            file=sys.stderr,
        )
        fallback = self._load_constituents_cache()
        if fallback is not None:
            constituents, cached_at = fallback
            print(
                f"WARNING: falling back to cached S&P 500 constituents from "
                f"{cached_at} ({len(constituents)} symbols)",
                file=sys.stderr,
            )
            self.cache[cache_key] = constituents
            return constituents
        return None

    def _fetch_datahub_constituents(self) -> Optional[list[dict]]:
        """Download and parse the DataHub S&P 500 constituents CSV.

        Maps the CSV columns to FMP's constituent shape so downstream callers
        (symbol/name/sector lookups) are unchanged. Returns None on any
        network or parse failure, recording the cause in ``last_error``.
        """
        try:
            response = requests.get(self._CONSTITUENTS_URL, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            self.last_error = f"DataHub request failed: {exc}"
            return None
        try:
            reader = csv.DictReader(io.StringIO(response.text))
            constituents = [
                {
                    "symbol": row["Symbol"].strip(),
                    "name": (row.get("Name") or row["Symbol"]).strip(),
                    "sector": (row.get("Sector") or "Unknown").strip(),
                    "subSector": (row.get("Sub-Industry") or "").strip(),
                }
                for row in reader
                if (row.get("Symbol") or "").strip()
            ]
        except (csv.Error, KeyError) as exc:
            self.last_error = f"DataHub CSV parse failed: {exc}"
            return None
        if not constituents:
            self.last_error = "DataHub CSV contained no rows"
            return None
        return constituents

    def _save_constituents_cache(self, constituents: list[dict]) -> None:
        """Persist a freshly-fetched constituent list as last-known-good."""
        try:
            self._CONSTITUENTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "constituents": constituents,
            }
            self._CONSTITUENTS_CACHE.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as e:
            print(
                f"WARNING: could not cache S&P 500 constituents: {e}",
                file=sys.stderr,
            )

    def _load_constituents_cache(self) -> Optional[tuple[list[dict], str]]:
        """Load the last-known-good constituent list, if present and valid."""
        try:
            raw = self._CONSTITUENTS_CACHE.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
            constituents = payload["constituents"]
            cached_at = payload.get("cached_at", "unknown time")
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if not isinstance(constituents, list) or not constituents:
            return None
        return constituents, cached_at

    def get_quote(self, symbols: str) -> Optional[list[dict]]:
        """Fetch real-time quote data for one or more symbols (comma-separated)"""
        cache_key = f"quote_{symbols}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        data = self._request_with_fallback("quote", symbols)
        if data:
            self.cache[cache_key] = data
        return data

    def get_historical_prices(self, symbol: str, days: int = 365) -> Optional[dict]:
        """Fetch historical daily OHLCV data"""
        cache_key = f"prices_{symbol}_{days}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        data = self._request_with_fallback("historical", symbol, {"timeseries": days})
        if data:
            self.cache[cache_key] = data
        return data

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch quotes one symbol at a time (free plan limit)."""
        results = {}
        for sym in symbols:
            quotes = self.get_quote(sym)
            if quotes:
                for q in quotes:
                    results[q["symbol"]] = q
        return results

    def get_batch_historical(
        self, symbols: list[str], days: int = 260
    ) -> dict[str, list[dict]]:
        """Fetch historical prices for multiple symbols"""
        results = {}
        for symbol in symbols:
            data = self.get_historical_prices(symbol, days=days)
            if data and "historical" in data:
                results[symbol] = data["historical"]
        return results

    def calculate_sma(self, prices: list[float], period: int) -> float:
        """Calculate Simple Moving Average from a list of prices (most recent first)"""
        if len(prices) < period:
            return sum(prices) / len(prices)
        return sum(prices[:period]) / period

    def get_api_stats(self) -> dict:
        return {
            "cache_entries": len(self.cache),
            "api_calls_made": self.api_calls_made,
            "rate_limit_reached": self.rate_limit_reached,
        }
