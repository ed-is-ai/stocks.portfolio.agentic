"""Alpha Vantage API client for fundamental data.

Used as a fallback when yfinance returns None for key fields.
Free tier: 25 API calls/day, 5 calls/minute.

Supported endpoints:
  - OVERVIEW  → sector, PE, ROE, quarterly EPS growth, annual EPS growth
  - EARNINGS  → full quarterly/annual EPS history for precise YoY calculation

Set ALPHA_VANTAGE_API_KEY in your .env to enable.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests


_BASE_URL = "https://www.alphavantage.co/query"
_MIN_CALL_INTERVAL = 12.0  # 5 calls/min → 12 s between calls


class AlphaVantageClient:
    """Rate-limited Alpha Vantage client with per-session memory cache."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY")
        self._cache: dict[str, Any] = {}
        self._last_call: float = 0.0
        self._call_attempts = 0
        self._call_failures = 0

    @property
    def enabled(self) -> bool:
        """Return True when an API key is configured."""
        return bool(self.api_key)

    def reset_call_stats(self) -> None:
        """Reset the per-run request/failure counters used for source health.

        Call once at the start of a scan so ``call_stats`` reflects only
        this run's requests, not a running total across the process
        lifetime (the client is a long-lived singleton).
        """
        self._call_attempts = 0
        self._call_failures = 0

    @property
    def call_stats(self) -> tuple[int, int]:
        """Return (attempts, failures) since the last ``reset_call_stats()``.

        A "failure" is a request that errored, timed out, or came back with
        a rate-limit/invalid-key message — distinct from a request that
        succeeded but had no data for that symbol. Without this distinction
        a provider outage and a quiet, fully-successful run both surface as
        "no data", which is exactly the ambiguity #40 asks to remove.
        """
        return self._call_attempts, self._call_failures

    def _get(self, params: dict[str, str]) -> dict[str, Any] | None:
        """Issue a rate-limited GET request and return the JSON body."""
        if not self.enabled:
            return None

        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)

        params["apikey"] = self.api_key  # type: ignore[assignment]
        self._call_attempts += 1
        try:
            resp = requests.get(_BASE_URL, params=params, timeout=15)
            self._last_call = time.monotonic()
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            if "Note" in data or "Information" in data:
                # Rate-limit or invalid key message from Alpha Vantage
                msg = data.get("Note") or data.get("Information", "")
                print(f"[alpha_vantage] API warning: {msg[:120]}")
                self._call_failures += 1
                return None
            return data
        except Exception as exc:
            print(f"[alpha_vantage] Request error: {exc}")
            self._call_failures += 1
            return None

    def get_overview(self, symbol: str) -> dict[str, Any] | None:
        """Fetch company OVERVIEW for the given ticker (cached per session)."""
        cache_key = f"overview:{symbol}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        data = self._get({"function": "OVERVIEW", "symbol": symbol})
        if data and data.get("Symbol"):
            self._cache[cache_key] = data
            return data
        return None

    def get_earnings(self, symbol: str) -> dict[str, Any] | None:
        """Fetch quarterly and annual earnings for the given ticker (cached)."""
        cache_key = f"earnings:{symbol}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        data = self._get({"function": "EARNINGS", "symbol": symbol})
        if data and (data.get("quarterlyEarnings") or data.get("annualEarnings")):
            self._cache[cache_key] = data
            return data
        return None

    # ------------------------------------------------------------------
    # Higher-level helpers
    # ------------------------------------------------------------------

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """Return a dict of fundamental fields for *symbol* using OVERVIEW only.

        Keys match the field names expected by scanner_agent._fetch_fundamentals:
          eps_growth, annual_eps_growth, roe, pe_ratio, sector
        All values may be None on failure or missing data.
        """
        empty: dict[str, Any] = {
            "eps_growth": None,
            "annual_eps_growth": None,
            "roe": None,
            "pe_ratio": None,
            "sector": None,
        }
        overview = self.get_overview(symbol)
        if not overview:
            return empty

        def _pct(key: str) -> float | None:
            """Parse a percentage-string field to a decimal (e.g. '0.253' → 0.253)."""
            raw = overview.get(key, "None")
            if not raw or raw in ("None", "-", "N/A"):
                return None
            try:
                return round(float(raw), 4)
            except ValueError:
                return None

        return {
            "eps_growth": _pct("QuarterlyEarningsGrowthYOY"),
            "annual_eps_growth": _pct(
                "EPS"
            ),  # TTM EPS; CAGR computed separately if needed
            "roe": _pct("ReturnOnEquityTTM"),
            "pe_ratio": _pct("PERatio"),
            "sector": overview.get("Sector") or None,
        }

    def get_quarterly_eps_growth(self, symbol: str) -> float | None:
        """Return MRQ YoY EPS growth from EARNINGS endpoint.

        More precise than OVERVIEW's QuarterlyEarningsGrowthYOY because it
        computes from the actual reported EPS figures (same logic as yfinance path).
        Returns None when fewer than 5 quarterly data points are available or
        when the year-ago quarter had negative/zero EPS.
        """
        earnings = self.get_earnings(symbol)
        if not earnings:
            return None
        quarters: list[dict[str, str]] = earnings.get("quarterlyEarnings", [])
        if len(quarters) < 5:
            return None
        try:
            recent = float(quarters[0].get("reportedEPS", "None"))
            year_ago = float(quarters[4].get("reportedEPS", "None"))
        except (ValueError, TypeError):
            return None
        if year_ago <= 0:
            return None
        return round((recent - year_ago) / year_ago, 4)

    def get_annual_eps_cagr(self, symbol: str) -> float | None:
        """Return 3-year annual EPS CAGR from EARNINGS endpoint.

        Uses up to 4 annual data points (3 growth periods). Returns None when
        insufficient data or when the base-year EPS was negative.
        """
        earnings = self.get_earnings(symbol)
        if not earnings:
            return None
        annual: list[dict[str, str]] = earnings.get("annualEarnings", [])
        n = min(len(annual), 4)
        if n < 3:
            return None
        try:
            newest = float(annual[0].get("reportedEPS", "None"))
            oldest = float(annual[n - 1].get("reportedEPS", "None"))
        except (ValueError, TypeError):
            return None
        if oldest <= 0 or newest <= 0:
            return None
        cagr = (newest / oldest) ** (1 / (n - 1)) - 1
        return round(cagr, 4)
