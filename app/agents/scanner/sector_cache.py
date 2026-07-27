"""Persistent last-known GICS sector per ticker.

yfinance's ``.info`` is rate-limited: under load a chunk of tickers return an
empty payload on a given run, dropping ``sector`` (and every other
fundamental) to None even for well-known equities. This cache remembers the
last non-null sector seen for each ticker so a throttled run can reuse it
instead of writing null. Sector rarely changes, so reuse is safe.
"""

from __future__ import annotations

import json
from pathlib import Path


class SectorCache:
    """A ticker -> sector map backed by a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sectors: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """Load the cache, tolerating a missing or corrupt file."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, str) and v.strip()}

    def get(self, ticker: str) -> str | None:
        """Return the last-known sector for ``ticker``, or None if unseen."""
        return self._sectors.get(ticker)

    def remember(self, ticker: str, sector: str | None) -> bool:
        """Record a fresh non-null sector; return True if the cache changed."""
        if not sector or self._sectors.get(ticker) == sector:
            return False
        self._sectors[ticker] = sector
        return True

    def save(self) -> None:
        """Persist the cache to disk, sorted for stable diffs."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._sectors, indent=2, sort_keys=True),
            encoding="utf-8",
        )
