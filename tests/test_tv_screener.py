"""Tests for the TradingView screener helpers (no network)."""

from __future__ import annotations

import math

from app.integrations.tv_screener import map_sector


class TestMapSector:
    """TradingView TRBC sectors map to the app's GICS vocabulary (#122)."""

    def test_known_trbc_sectors_map_to_gics(self) -> None:
        assert map_sector("Technology Services") == "Technology"
        assert map_sector("Electronic Technology") == "Technology"
        assert map_sector("Finance") == "Financial Services"
        assert map_sector("Health Technology") == "Healthcare"
        assert map_sector("Retail Trade") == "Consumer Cyclical"
        assert map_sector("Energy Minerals") == "Energy"
        assert map_sector("Utilities") == "Utilities"
        assert map_sector("Communications") == "Communication Services"

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert map_sector("  Finance  ") == "Financial Services"

    def test_unmapped_or_nonstring_returns_none(self) -> None:
        assert map_sector("Miscellaneous") is None
        assert map_sector("") is None
        assert map_sector(None) is None
        assert map_sector(math.nan) is None
