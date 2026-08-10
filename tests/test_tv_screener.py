"""Tests for the TradingView screener helpers (no network)."""

from __future__ import annotations

import math

import pandas as pd

from app.integrations.tv_screener import _extract_roster_rows, map_sector


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


def test_roster_rows_retain_exchange_qualified_symbol_and_currency() -> None:
    frame = pd.DataFrame(
        {
            "name": ["NASDAQ:AAPL", "NYSE:IBM"],
            "exchange": ["NASDAQ", "NYSE"],
            "currency": ["USD", "USD"],
            "sector": ["Technology Services", "Technology Services"],
        }
    )
    assert _extract_roster_rows(frame, is_uk=False) == (
        {
            "symbol": "NASDAQ:AAPL",
            "exchange": "NASDAQ",
            "currency": "USD",
            "quote_unit": "USD",
        },
        {
            "symbol": "NYSE:IBM",
            "exchange": "NYSE",
            "currency": "USD",
            "quote_unit": "USD",
        },
    )

    uk = pd.DataFrame(
        {
            "name": ["LSE:ULVR"],
            "exchange": ["LSE"],
            "currency": ["GBp"],
            "sector": ["Consumer Non-Durables"],
        }
    )
    assert _extract_roster_rows(uk, is_uk=True) == (
        {
            "symbol": "LSE:ULVR",
            "exchange": "LSE",
            "currency": "GBP",
            "quote_unit": "GBp",
        },
    )
