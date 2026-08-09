"""Realised P&L schemas — FIFO round-trip matching output (Epic 1).

Populated by ``app.services.realised_pnl_service.RealisedPnlService``.
"""

from __future__ import annotations

from pydantic import BaseModel


class RoundTrip(BaseModel):
    """One closed lot: a SELL matched FIFO against a single BUY lot.

    ``realised_pnl_gbp``/``realised_pnl_pct`` are GBP amounts, converted
    per-leg at each leg's own trade-date GBP/USD rate (Story 1.2). When
    ``fx_unavailable`` is ``True`` (a leg's rate couldn't be resolved), both
    fields hold a documented ``0.0`` placeholder, never a real figure —
    every caller must check ``fx_unavailable`` first and exclude the row
    from any subtotal or total.
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


class UnmatchedSell(BaseModel):
    """A SELL (or the unconsumed remainder of one) with no open BUY lot to
    draw from — a genuine data gap (transfer-in, missing buy), never a
    fabricated match (Story 1.3).

    ``acknowledged_at`` defaults to unset on every record this story
    produces; Story 1.5 owns writing/reading it from
    ``trades.realised_pnl_ack_at``.

    ``price`` is the trade's raw, native-currency price (unlike
    ``RoundTrip.realised_pnl_gbp``, this is intentionally *not*
    GBP-converted) — an unmatched sell never contributes to a P&L total, so
    there is nothing to convert; showing the ticker's own listing price is
    the figure the user recognises from their broker statement.
    """

    trade_id: int
    ticker: str
    portfolio_id: int
    date: str
    shares: float
    price: float
    """Native-currency price (see class docstring) — never GBP-converted."""
    reason: str
    acknowledged_at: str | None = None


class RealisedPnlSummary(BaseModel):
    """Account-level Realised P&L result for one ``compute_summary`` call.

    ``round_trips`` is keyed by ticker; dict insertion order reflects AD-9's
    group ordering (most-recent exit date first) — callers/templates must
    render this order as given, never re-sort. ``unmatched_sells`` is a
    separate, ungrouped list (Story 1.3) and is never merged into
    ``round_trips``.
    """

    portfolio_id: int
    round_trips: dict[str, list[RoundTrip]] = {}
    total_realised_pnl_gbp: float
    round_trip_count: int
    unmatched_count: int = 0
    unmatched_sells: list[UnmatchedSell] = []
    mismatched_tickers: list[str] = []
