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


class SkippedInvalidDateTrade(BaseModel):
    """A trade row FIFO replay excluded because ``date`` failed
    ``date.fromisoformat`` (Story 2.2).

    Makes the skip retrievable as structured data -- not only a log line --
    for a future Match Trace (Story 2.4) to surface. ``raw_date`` is the
    exact, unparsed value stored on the trade row.
    """

    trade_id: int
    ticker: str
    raw_date: str
    reason: str


class MatchTraceCandidateLot(BaseModel):
    """One BUY lot FIFO drew from while matching a sell (Story 2.4).

    ``is_opening_lot`` mirrors ``source == "opening_lot"`` so the UI can
    render the "Manually entered" label (AC6) without a second lookup.
    """

    trade_id: int
    buy_date: str
    buy_price: float
    shares_consumed: float
    source: str | None = None
    import_batch_id: str | None = None
    is_opening_lot: bool = False


class MatchTrace(BaseModel):
    """Full explanation of how one SELL was (or wasn't) FIFO-matched
    (Story 2.4) -- everything ``_replay_fifo`` already computes for this
    sell while matching it, surfaced on demand via
    ``RealisedPnlService.get_match_trace`` instead of folded into
    ``UnmatchedSell``'s coarse ``reason`` string.

    Sibling to ``UnmatchedSell``, not a replacement for it: the summary
    list still shows the coarse reason; this is the dedicated detail view
    (mirrors the ``RealisedPnlSummary.unmatched_sells`` + dedicated
    reconciliation-view shape already used elsewhere in this app).

    AC2's duplicate/overlapping-import case is only partially satisfiable:
    ``source``/``import_batch_id`` show which import produced *this sell*,
    never a specific rejected/deduped row from a *different* import --
    that data is never persisted anywhere queryable (see
    ``RealisedPnlService.get_match_trace`` docstring). This is a
    documented limitation, not a defect.
    """

    trade_id: int
    ticker: str
    """Canonicalized identity (Story 2.1) -- the FIFO queue key this sell
    matched against, not necessarily its raw stored spelling."""
    portfolio_id: int
    date: str
    shares: float
    """Total shares on the sell trade itself."""
    price: float
    """Native-currency price (never GBP-converted), matching ``UnmatchedSell.price``."""
    shares_matched: float
    """Sum of ``shares_consumed`` across ``candidate_lots``."""
    shares_unmatched: float
    """Remaining shortfall this sell couldn't match against any lot --
    ``0.0`` for a sell that matched cleanly."""
    candidate_lots: list[MatchTraceCandidateLot] = []
    ordering_note: str
    """The Story 2.2 chronological/tie-break rule applied, with the actual
    ``source_row_index``/``idempotency_key`` values this sell carried --
    the values used, not a re-derivation of the sort."""
    skipped_invalid_date_trades: list[SkippedInvalidDateTrade] = []
    """Trades for this ticker FIFO excluded for an unparseable date
    (Story 2.2) -- could explain a missing lot."""
    source: str | None = None
    """This sell trade's own provenance (which import, if any, produced it)."""
    import_batch_id: str | None = None
    reason: str | None = None
    """The coarse ``UnmatchedSell.reason`` text, when this sell has a
    shortfall -- ``None`` for a sell that matched cleanly."""


class RealisedPnlSummary(BaseModel):
    """Account-level Realised P&L result for one ``compute_summary`` call.

    ``round_trips`` is keyed by ticker; dict insertion order reflects AD-9's
    group ordering (most-recent exit date first) — callers/templates must
    render this order as given, never re-sort. ``unmatched_sells`` is a
    separate, ungrouped list (Story 1.3) and is never merged into
    ``round_trips``. ``skipped_invalid_date_trades`` (Story 2.2) is a
    similarly separate, ungrouped list of trades FIFO excluded for having an
    unparseable date.
    """

    portfolio_id: int
    round_trips: dict[str, list[RoundTrip]] = {}
    total_realised_pnl_gbp: float
    round_trip_count: int
    winning_round_trip_count: int = 0
    losing_round_trip_count: int = 0
    average_win_pct: float | None = None
    """Simple mean for the resolved GBP win bucket; ``None`` when empty."""
    average_loss_pct: float | None = None
    """Simple mean for the resolved GBP loss bucket; ``None`` when empty."""
    unmatched_count: int = 0
    unmatched_sells: list[UnmatchedSell] = []
    mismatched_tickers: list[str] = []
    skipped_invalid_date_trades: list[SkippedInvalidDateTrade] = []
