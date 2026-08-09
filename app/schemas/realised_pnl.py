"""Realised P&L schemas — FIFO round-trip matching output (Epic 1).

Populated by ``app.services.realised_pnl_service.RealisedPnlService``.
"""

from __future__ import annotations

from pydantic import BaseModel


class RoundTrip(BaseModel):
    """One closed lot: a SELL matched FIFO against a single BUY lot.

    ``realised_pnl_gbp``/``realised_pnl_pct`` are native-currency placeholders
    in Story 1.1 (treat ``price`` as if already GBP); Story 1.2 replaces the
    calculation with true trade-date FX conversion without changing these
    field names. ``fx_unavailable`` is always ``False`` until Story 1.2 adds
    FX lookups.
    """

    ticker: str
    portfolio_id: int
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: float
    holding_period_days: int
    realised_pnl_gbp: float
    realised_pnl_pct: float
    fx_unavailable: bool = False


class RealisedPnlSummary(BaseModel):
    """Account-level Realised P&L result for one ``compute_summary`` call.

    ``round_trips`` is keyed by ticker; dict insertion order reflects AD-9's
    group ordering (most-recent exit date first) — callers/templates must
    render this order as given, never re-sort.
    """

    portfolio_id: int
    round_trips: dict[str, list[RoundTrip]] = {}
    total_realised_pnl_gbp: float
    round_trip_count: int
    unmatched_count: int = 0
    mismatched_tickers: list[str] = []


# UnmatchedSell is added by Story 1.3.
