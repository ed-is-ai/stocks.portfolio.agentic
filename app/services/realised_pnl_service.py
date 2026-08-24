"""Realised P&L service — FIFO lot matching, round-trip shaping, trade-date
GBP conversion, and unmatched-sell detection.

Epic 1, Story 1.1 (FIFO matching) + Story 1.2 (trade-date FX conversion with
historical rate caching) + Story 1.3 (unmatched-sell detection).
Acknowledgment (Story 1.5) is a later story layered on top without changing
this story's schema fields.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.core.quantity import QUANTITY_EPSILON, round_quantity
from app.core.ticker_identity import canonicalize_or_fallback
from app.schemas import (
    MatchTrace,
    MatchTraceCandidateLot,
    RealisedPnlSummary,
    RoundTrip,
    SkippedInvalidDateTrade,
    Trade,
    UnmatchedSell,
)
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)

_INVALID_TICKERS = {"", "n/a", "N/A"}

# Story 1.3: two reason strings depending on whether any prior lot existed
# at all for this SELL, vs. whether one or more lots existed and were
# partially consumed before running out -- "no prior BUY found" is
# factually wrong for the second case (a BUY plainly *was* found).
_NO_PRIOR_BUY_REASON = "No prior BUY found to match this sell"
_PARTIAL_SHORTFALL_REASON = "Sell exceeds available BUY lots by {shares:g} shares"

# Story 2.2: reason text for a trade excluded from FIFO replay because its
# stored ``date`` isn't valid ISO-8601 -- surfaced both in the log line and
# in the retrievable ``SkippedInvalidDateTrade`` structure.
_INVALID_DATE_REASON = "Trade date {raw_date!r} is not valid ISO-8601"


def _round2(x: float) -> float:
    """Round a GBP money amount to 2dp — the single money-rounding rule."""
    return round(x, 2)


def _ordering_note(t: Trade) -> str:
    """Describe Story 2.2's chronological/tie-break replay rule for the
    Match Trace, using the *values this trade itself carried* into
    ``_replay_sort_key`` -- exposition, not a re-derivation of the sort."""
    return (
        "Chronological replay: sorted by date, then (within the same "
        "date) by descending source_row_index (NULL treated as -1, so an "
        "unindexed row replays last in its date group), then "
        "idempotency_key, then id. This trade's own "
        f"source_row_index={t.source_row_index!r}, "
        f"idempotency_key={t.idempotency_key!r}."
    )


@dataclass
class _Lot:
    """One open BUY lot in a per-ticker FIFO queue."""

    shares_remaining: float
    buy_price: float
    buy_date: str
    # Story 2.4: the id of the BUY trade this lot came from. Required to
    # identify a *specific* lot after a fresh replay for both the Match
    # Trace (which BUY a candidate lot came from) and the Opening Lot
    # consumed/unconsumed check (ticker+date+price alone can collide, e.g.
    # two same-day/same-price BUYs of one ticker).
    trade_id: int | None = None


@dataclass
class _RawRoundTrip:
    """One matched (SELL, lot) pair before GBP conversion.

    Entry/exit price are still in the ticker's native currency; GBP
    conversion happens afterward in ``_convert_to_gbp`` (Story 1.2), once
    per distinct trade date across the whole batch rather than per leg.
    """

    ticker: str
    portfolio_id: int
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: float
    holding_period_days: int


class RealisedPnlService:
    """FIFO-matches SELLs against the oldest open BUY lots per ticker, then
    converts each Round-trip's legs to GBP at their own trade-date rate.

    Reads trade rows via ``TraderService.get_trade_history()``, which
    returns newest-first, then sorts them ascending by ``(date, id)`` and
    drops non-positive-share / invalid-ticker rows itself (AD-4) — this is
    the same ordering/filter ``TradesRepository.open_rows()`` applies for
    the average-cost replay, reimplemented independently here. This service
    must never call or import the Portfolio tab's average-cost matching
    method on ``TraderAgent`` (it stays untouched).

    Currency and FX resolution go exclusively through ``PortfolioService``
    (``ticker_currency``/``historical_gbpusd_rates``, AD-5) — this service
    never fetches market data or the live rate itself.

    No persistence or caching of any kind: every ``compute_summary`` call
    recomputes fresh from live trade data.
    """

    def __init__(
        self, trader_service: TraderService, portfolio_service: PortfolioService
    ) -> None:
        self._trader = trader_service
        self._portfolio = portfolio_service

    def compute_summary(self, portfolio_id: int) -> RealisedPnlSummary:
        """Return a fully-populated Realised P&L summary for one Account.

        Recomputed from scratch on every call — nothing is cached or
        persisted (AC 6).
        """
        trades, skipped_invalid_dates = self._sorted_valid_trades(portfolio_id)
        raw_round_trips, open_lots, unmatched_sells, _traces = self._replay_fifo(
            trades, portfolio_id
        )
        round_trips = self._convert_to_gbp(raw_round_trips)
        grouped = self._group_and_order(round_trips)
        mismatched = self._mismatched_tickers(open_lots, unmatched_sells, portfolio_id)
        # FX-unavailable Round-trips carry a 0.0 placeholder, not a real
        # figure, and must never enter the Account total (Story 1.2 AC7).
        total_pnl = _round2(
            sum(rt.realised_pnl_gbp for rt in round_trips if not rt.fx_unavailable)
        )
        resolved_round_trips = [rt for rt in round_trips if not rt.fx_unavailable]
        winning_round_trips = [
            rt for rt in resolved_round_trips if rt.realised_pnl_gbp >= 0
        ]
        losing_round_trips = [
            rt for rt in resolved_round_trips if rt.realised_pnl_gbp < 0
        ]
        return RealisedPnlSummary(
            portfolio_id=portfolio_id,
            round_trips=grouped,
            total_realised_pnl_gbp=total_pnl,
            round_trip_count=len(round_trips),
            winning_round_trip_count=len(winning_round_trips),
            losing_round_trip_count=len(losing_round_trips),
            average_win_pct=(
                sum(rt.realised_pnl_pct for rt in winning_round_trips)
                / len(winning_round_trips)
                if winning_round_trips
                else None
            ),
            average_loss_pct=(
                sum(rt.realised_pnl_pct for rt in losing_round_trips)
                / len(losing_round_trips)
                if losing_round_trips
                else None
            ),
            unmatched_count=len(unmatched_sells),
            unmatched_sells=unmatched_sells,
            mismatched_tickers=mismatched,
            skipped_invalid_date_trades=skipped_invalid_dates,
        )

    def toggle_unmatched_sell_ack(
        self, trade_id: int, portfolio_id: int
    ) -> RealisedPnlSummary:
        """Flip one unmatched sell's acknowledgment and return the fresh summary.

        Looks up the sell's current ``acknowledged_at`` in a freshly
        computed summary and writes the opposite (AD-8: pure toggle of
        current state, no acknowledged value accepted from the caller). If
        ``trade_id`` isn't among the current unmatched sells (e.g. a stale
        click), this is a no-op — no exception, no write. Recomputes after
        writing, since Round-trip/unmatched-sell results are never cached
        (AD-7).
        """
        summary = self.compute_summary(portfolio_id)
        target = next(
            (u for u in summary.unmatched_sells if u.trade_id == trade_id), None
        )
        if target is not None:
            self._trader.set_unmatched_sell_ack(
                trade_id, target.acknowledged_at is None
            )
            summary = self.compute_summary(portfolio_id)
        return summary

    def get_match_trace(self, trade_id: int, portfolio_id: int) -> MatchTrace | None:
        """Return the full FIFO match explanation for one SELL trade
        (Story 2.4) -- fetch-on-demand detail, mirroring this codebase's
        existing "summary list + dedicated detail view" shape
        (``RealisedPnlSummary.unmatched_sells`` + this).

        Recomputed fresh from a full ``_replay_fifo`` run on every call --
        nothing cached, matching ``compute_summary``'s no-caching design
        (AC 6). Returns ``None`` if ``trade_id`` isn't a SELL trade found
        in this portfolio's replay (e.g. a stale link, a BUY's id, or a
        trade in a different portfolio).

        AC2's duplicate/overlapping-import case is only partially
        satisfiable: the returned trace's ``source``/``import_batch_id``
        show which import produced *this sell*, never a specific
        rejected/deduped row from a *different* import that may have
        caused a missing lot -- direct investigation confirmed per-row
        duplicate/skip/fail outcomes exist only transiently (the HTTP
        response body and one prose notification per import file), never
        persisted with a queryable link back to ``trades``. This is a
        documented, accepted limitation, not a defect to fabricate around.
        """
        trades, skipped = self._sorted_valid_trades(portfolio_id)
        _, _, _, traces = self._replay_fifo(trades, portfolio_id)
        trace = traces.get(trade_id)
        if trace is None:
            return None
        ticker_skips = [s for s in skipped if s.ticker == trace.ticker]
        return trace.model_copy(update={"skipped_invalid_date_trades": ticker_skips})

    def opening_lot_status(
        self, trade_id: int, portfolio_id: int
    ) -> Literal["unconsumed", "consumed"] | None:
        """Return whether an Opening Lot trade has been touched by FIFO
        matching (Story 2.4, AC7/AC8) -- never a persisted column.

        Runs a fresh ``_replay_fifo`` (matching ``compute_summary``'s
        no-caching design) and looks for the ``_Lot`` this trade produced,
        identified by its ``trade_id`` (not ticker/date/price, which two
        lots could share). A lot still present with ``shares_remaining``
        equal (within ``QUANTITY_EPSILON``) to the trade's own ``shares``
        was never touched by a SELL -- ``"unconsumed"``, safe to
        edit/delete. A lot present with a smaller ``shares_remaining`` is
        ``"consumed"`` (partially). A lot *absent* entirely is also
        ``"consumed"`` (fully) -- ``_replay_fifo`` dequeues a lot the
        instant its last share is matched, so "not found in the open-lot
        queues" is the fully-consumed case here, not a sign of a bad id.

        Returns ``None`` if ``trade_id`` doesn't exist in this portfolio's
        trade history at all (caller should treat this the same as a
        missing/invalid Opening Lot, distinct from ``"consumed"``).
        """
        all_trades = self._trader.get_trade_history(portfolio_id=portfolio_id)
        trade = next((t for t in all_trades if t.id == trade_id), None)
        if trade is None:
            return None
        valid_trades, _ = self._sorted_valid_trades(portfolio_id)
        _, open_lots, _, _ = self._replay_fifo(valid_trades, portfolio_id)
        aliases = self._portfolio.load_ticker_aliases()
        ticker = canonicalize_or_fallback(
            trade.ticker, aliases, logger=logger, context="opening_lot_status"
        )
        for lot in open_lots.get(ticker, ()):
            if lot.trade_id == trade.id:
                if abs(lot.shares_remaining - trade.shares) <= QUANTITY_EPSILON:
                    return "unconsumed"
                return "consumed"
        return "consumed"

    def _sorted_valid_trades(
        self, portfolio_id: int
    ) -> tuple[list[Trade], list[SkippedInvalidDateTrade]]:
        """Fetch, filter, and sort trades for FIFO replay.

        The only place trade rows are fetched/sorted for FIFO purposes.
        Must never call or import the average-cost replay method on
        ``TraderAgent``. A row whose ``date`` isn't valid ISO-8601 is
        skipped defensively (logged, not raised) — same tolerance
        philosophy as the shares/ticker filter below; this codebase has
        shipped and fixed real trade-date corruption before (#166, #167),
        so a malformed date reaching this far, while unexpected, must not
        crash the whole Account's Realised P&L computation. Every such skip
        is also collected into a returned, retrievable structure (Story
        2.2) — trade id, ticker, raw date, reason — alongside the existing
        log line, for a future Match Trace to surface.

        Sort key (Story 2.2, applied identically to the average-cost path
        in ``TradesRepository.open_rows``/``open_rows_on_connection``):
        ``date`` ascending, then same-day rows by descending
        ``source_row_index`` (the first-listed row in a source CSV's
        same-day group is the most recent execution, so chronological replay
        processes it last -- i.e. the highest index first), then
        ``idempotency_key`` as the content-derived cross-file tiebreak.
        ``source_row_index IS NULL`` (rows imported before this story
        shipped) is treated as the lowest possible position in its date
        group, so it replays last among same-day peers without crashing on
        ``None`` arithmetic/comparison.
        """
        trades = self._trader.get_trade_history(portfolio_id=portfolio_id)
        valid = []
        skipped: list[SkippedInvalidDateTrade] = []
        for t in trades:
            if t.shares <= 0 or t.ticker in _INVALID_TICKERS:
                continue
            try:
                date.fromisoformat(t.date)
            except ValueError:
                logger.warning(
                    "Realised P&L: skipping trade id=%s (%s) with unparseable date %r",
                    t.id,
                    t.ticker,
                    t.date,
                )
                if t.id is None:
                    logger.warning(
                        "Realised P&L: invalid-date trade for %s has no "
                        "trade id -- this should be unreachable for a "
                        "persisted row; falling back to trade_id=0",
                        t.ticker,
                    )
                skipped.append(
                    SkippedInvalidDateTrade(
                        trade_id=t.id or 0,
                        ticker=t.ticker,
                        raw_date=t.date,
                        reason=_INVALID_DATE_REASON.format(raw_date=t.date),
                    )
                )
                continue
            valid.append(t)
        ordered = sorted(valid, key=self._replay_sort_key)
        return ordered, skipped

    @staticmethod
    def _replay_sort_key(t: Trade) -> tuple[str, int, str, int]:
        """Deterministic same-day FIFO replay order (Story 2.2).

        See ``_sorted_valid_trades`` for the full rule; ``None`` positions
        are treated as ``-1`` (the lowest possible position) before
        negating, so a pre-Story-2.2 row without a ``source_row_index``
        never crashes the comparison and always sorts last within its date
        group. A missing ``idempotency_key`` falls back to ``""`` for the
        same reason.

        Trailing ``id`` tiebreak: rows written outside the SIPP import
        (e.g. ``record_buy``/``record_sell``) carry neither
        ``source_row_index`` nor ``idempotency_key``, so two such same-day
        rows would otherwise tie completely. Falling back to ascending
        ``id`` preserves this codebase's pre-existing, already-tested
        same-day tie-break for that case (AC 4) without reintroducing
        insertion order as a signal for rows that *do* carry real Story 2.2
        ordering evidence -- it only ever decides a tie the first three key
        elements left unresolved.
        """
        position = t.source_row_index if t.source_row_index is not None else -1
        return (t.date, -position, t.idempotency_key or "", t.id or 0)

    def _replay_fifo(
        self, trades: list[Trade], portfolio_id: int
    ) -> tuple[
        list[_RawRoundTrip],
        dict[str, deque[_Lot]],
        list[UnmatchedSell],
        dict[int, MatchTrace],
    ]:
        """Replay trades in order, FIFO-matching SELLs against open lots.

        On BUY, push a new lot onto that ticker's queue. On SELL, pop from
        the front, consuming up to ``shares_remaining`` per lot until the
        sold quantity is satisfied or the queue empties; a fully-consumed
        lot is dequeued immediately so it can never be re-matched. If the
        queue empties before a SELL is fully satisfied (including the
        fully-empty-queue case), the unconsumed remainder is emitted as an
        ``UnmatchedSell`` (Story 1.3) and matching for that SELL stops — no
        exception, no fabricated lot. This is a single forward pass over
        chronologically-sorted trades: an ``UnmatchedSell`` is never
        revisited or retroactively filled by a later BUY for the same
        ticker, because the SELL that produced it is never re-examined once
        the loop has moved past it.

        Each trade's ticker is canonicalized via ``canonicalize_or_fallback``
        before it keys ``queues`` -- the shared identity
        every ``RoundTrip``/``UnmatchedSell`` displays -- so cross-spelling
        trades for one security fold into a single FIFO queue instead of
        fragmenting, agreeing with the average-cost replay's identity. A
        cycle or malformed alias file degrades to the raw ticker with a
        logged warning rather than crashing this replay.

        Story 2.4: a ``MatchTrace`` is built for every SELL alongside this
        same loop (not recomputed in a second pass) and returned in the
        fourth element, keyed by the sell's own trade id -- one entry per
        SELL regardless of whether it matched cleanly, partially, or not
        at all. ``get_match_trace`` fetches from this dict on demand; a
        SELL with no persisted ``id`` (unreachable for a real row) is
        simply omitted, mirroring the same defensive ``t.id is None``
        tolerance the ``UnmatchedSell`` branch already has.
        """
        aliases = self._portfolio.load_ticker_aliases()
        # Every trade (BUY and SELL) keyed by id -- lets a SELL's trace look
        # up the *originating* BUY trade's own source/import_batch_id for
        # each candidate lot, purely by in-memory lookup (no second query).
        trades_by_id = {t.id: t for t in trades if t.id is not None}
        queues: dict[str, deque[_Lot]] = {}
        round_trips: list[_RawRoundTrip] = []
        unmatched_sells: list[UnmatchedSell] = []
        traces: dict[int, MatchTrace] = {}
        for t in trades:
            ticker = canonicalize_or_fallback(
                t.ticker, aliases, logger=logger, context="_replay_fifo"
            )
            queue = queues.setdefault(ticker, deque())
            if t.action == "BUY":
                queue.append(_Lot(t.shares, t.price, t.date, t.id))
                continue
            remaining_to_sell = t.shares
            any_lot_matched = False
            candidate_lots: list[MatchTraceCandidateLot] = []
            while remaining_to_sell > QUANTITY_EPSILON and queue:
                lot = queue[0]
                matched = min(lot.shares_remaining, remaining_to_sell)
                round_trips.append(
                    self._build_raw_round_trip(t, lot, matched, portfolio_id, ticker)
                )
                buy_trade = (
                    trades_by_id.get(lot.trade_id) if lot.trade_id is not None else None
                )
                candidate_lots.append(
                    MatchTraceCandidateLot(
                        trade_id=lot.trade_id or 0,
                        buy_date=lot.buy_date,
                        buy_price=lot.buy_price,
                        shares_consumed=matched,
                        source=buy_trade.source if buy_trade else None,
                        import_batch_id=(
                            buy_trade.import_batch_id if buy_trade else None
                        ),
                        is_opening_lot=bool(
                            buy_trade and buy_trade.source == "opening_lot"
                        ),
                    )
                )
                lot.shares_remaining -= matched
                remaining_to_sell -= matched
                any_lot_matched = True
                if lot.shares_remaining <= QUANTITY_EPSILON:
                    queue.popleft()
            remaining_to_sell = round_quantity(remaining_to_sell)
            reason: str | None = None
            if remaining_to_sell > QUANTITY_EPSILON:
                if t.id is None:
                    logger.warning(
                        "Realised P&L: unmatched SELL for %s has no trade id "
                        "-- this should be unreachable for a persisted row; "
                        "falling back to trade_id=0",
                        ticker,
                    )
                reason = (
                    _PARTIAL_SHORTFALL_REASON.format(shares=remaining_to_sell)
                    if any_lot_matched
                    else _NO_PRIOR_BUY_REASON
                )
                unmatched_sells.append(
                    UnmatchedSell(
                        trade_id=t.id or 0,
                        ticker=ticker,
                        portfolio_id=portfolio_id,
                        date=t.date,
                        shares=remaining_to_sell,
                        price=t.price,
                        reason=reason,
                        acknowledged_at=t.realised_pnl_ack_at,
                    )
                )
            if t.id is not None:
                traces[t.id] = MatchTrace(
                    trade_id=t.id,
                    ticker=ticker,
                    portfolio_id=portfolio_id,
                    date=t.date,
                    shares=t.shares,
                    price=t.price,
                    shares_matched=round_quantity(t.shares - remaining_to_sell),
                    shares_unmatched=remaining_to_sell,
                    candidate_lots=candidate_lots,
                    ordering_note=_ordering_note(t),
                    source=t.source,
                    import_batch_id=t.import_batch_id,
                    reason=reason,
                )
        return round_trips, queues, unmatched_sells, traces

    @staticmethod
    def _build_raw_round_trip(
        sell: Trade, lot: _Lot, matched_shares: float, portfolio_id: int, ticker: str
    ) -> _RawRoundTrip:
        """Build one raw (pre-GBP-conversion) Round-trip for a lot (or
        partial lot) consumed by a SELL. Entry/exit price stay in the
        ticker's native currency here; GBP conversion is a separate pass
        (``_convert_to_gbp``) so trade dates can be batch-resolved once
        across every Round-trip instead of per leg (Story 1.2 AC1/AC2).

        ``ticker`` is the caller's already-canonicalized identity (not
        ``sell.ticker``, the raw persisted spelling) -- ``_replay_fifo``
        resolves it once per trade and passes it in explicitly so this
        method never has to re-canonicalize or risk disagreeing with the
        queue key the caller used.
        """
        holding_days = (
            date.fromisoformat(sell.date) - date.fromisoformat(lot.buy_date)
        ).days
        return _RawRoundTrip(
            ticker=ticker,
            portfolio_id=portfolio_id,
            entry_date=lot.buy_date,
            entry_price=lot.buy_price,
            exit_date=sell.date,
            exit_price=sell.price,
            shares=matched_shares,
            holding_period_days=holding_days,
        )

    def _convert_to_gbp(self, raw_round_trips: list[_RawRoundTrip]) -> list[RoundTrip]:
        """Convert every raw Round-trip's legs to GBP at their own
        trade-date FX rate (Story 1.2, AC1/AC5/AC6).

        Resolves each distinct ticker's currency exactly once via
        ``PortfolioService.ticker_currency`` (the sole currency seam,
        AD-5), then batch-fetches every distinct trade date once per
        supported foreign currency. A GBP-currency ticker never needs a
        rate lookup at all.
        """
        currencies: dict[str, str] = {}
        for raw in raw_round_trips:
            if raw.ticker not in currencies:
                currencies[raw.ticker] = self._portfolio.ticker_currency(raw.ticker)

        dates_by_currency: dict[str, set[str]] = {"USD": set(), "HKD": set()}
        for raw in raw_round_trips:
            dates = dates_by_currency.get(currencies[raw.ticker])
            if dates is not None:
                dates.add(raw.entry_date)
                dates.add(raw.exit_date)
        rates_by_currency = {
            "USD": self._portfolio.historical_gbpusd_rates(
                sorted(dates_by_currency["USD"])
            )
        }
        if dates_by_currency["HKD"]:
            rates_by_currency["HKD"] = self._portfolio.historical_fx_rates(
                "HKD", sorted(dates_by_currency["HKD"])
            )

        return [
            self._convert_round_trip(
                raw,
                currencies[raw.ticker],
                rates_by_currency.get(currencies[raw.ticker], {}),
            )
            for raw in raw_round_trips
        ]

    @staticmethod
    def _convert_round_trip(
        raw: _RawRoundTrip, currency: str, rates: dict[str, float]
    ) -> RoundTrip:
        """Convert one raw Round-trip's legs to GBP and compute P&L/% on
        the GBP amounts (Story 1.2 AC1/AC5/AC7/AC8).

        A GBP-currency ticker's legs use rate = 1 (no lookup, no
        conversion — used as-is). A supported foreign-currency ticker's
        BUY/SELL legs each convert independently at their own trade date's
        rate. Any other currency is immediately ``fx_unavailable`` -- it must
        never fall through to a ``rates.get(...)`` lookup, which would
        silently apply an unrelated USD-leg's rate to a same-date
        third-currency trade. If either leg's rate is missing or fails the
        ``>0``/not-``None`` check, the whole Round-trip is flagged
        ``fx_unavailable`` with a documented ``0.0`` placeholder for the
        P&L fields (never a real figure — every caller must check
        ``fx_unavailable`` first and skip the row, e.g. ``compute_summary``'s
        own total).
        """
        if currency == "GBP":
            entry_rate: float | None = 1.0
            exit_rate: float | None = 1.0
        elif currency in {"USD", "HKD"}:
            entry_rate = rates.get(raw.entry_date)
            exit_rate = rates.get(raw.exit_date)
        else:
            logger.warning(
                "Realised P&L: unsupported currency %r for %s -- flagging "
                "fx_unavailable",
                currency,
                raw.ticker,
            )
            entry_rate = exit_rate = None

        if entry_rate is None or entry_rate <= 0 or exit_rate is None or exit_rate <= 0:
            return RoundTrip(
                ticker=raw.ticker,
                portfolio_id=raw.portfolio_id,
                entry_date=raw.entry_date,
                entry_price=raw.entry_price,
                exit_date=raw.exit_date,
                exit_price=raw.exit_price,
                shares=raw.shares,
                holding_period_days=raw.holding_period_days,
                realised_pnl_gbp=0.0,
                realised_pnl_pct=0.0,
                fx_unavailable=True,
            )

        gbp_cost = _round2(raw.entry_price * raw.shares / entry_rate)
        gbp_proceeds = _round2(raw.exit_price * raw.shares / exit_rate)
        pnl_gbp = _round2(gbp_proceeds - gbp_cost)
        pnl_pct = round(pnl_gbp / gbp_cost * 100, 2) if gbp_cost > 0 else 0.0
        return RoundTrip(
            ticker=raw.ticker,
            portfolio_id=raw.portfolio_id,
            entry_date=raw.entry_date,
            entry_price=raw.entry_price,
            exit_date=raw.exit_date,
            exit_price=raw.exit_price,
            shares=raw.shares,
            holding_period_days=raw.holding_period_days,
            realised_pnl_gbp=pnl_gbp,
            realised_pnl_pct=pnl_pct,
            fx_unavailable=False,
        )

    @staticmethod
    def _group_and_order(round_trips: list[RoundTrip]) -> dict[str, list[RoundTrip]]:
        """Group Round-trips by ticker and order per AD-9.

        Each ticker's group is sorted by ``exit_date`` descending; the
        groups themselves are ordered by each group's most-recent
        ``exit_date`` descending. The returned dict's insertion order *is*
        the group order — callers/templates render it as given, never
        re-sort.
        """
        by_ticker: dict[str, list[RoundTrip]] = {}
        for rt in round_trips:
            by_ticker.setdefault(rt.ticker, []).append(rt)
        for group in by_ticker.values():
            group.sort(key=lambda rt: rt.exit_date, reverse=True)
        ordered_tickers = sorted(
            by_ticker, key=lambda tk: by_ticker[tk][0].exit_date, reverse=True
        )
        return {tk: by_ticker[tk] for tk in ordered_tickers}

    def _mismatched_tickers(
        self,
        open_lots: dict[str, deque[_Lot]],
        unmatched_sells: list[UnmatchedSell],
        portfolio_id: int,
    ) -> list[str]:
        """Cross-check FIFO's net position against the avg-cost replay.

        Per AD-10, a ticker is reported only when FIFO's total disagrees
        with ``TraderService.get_portfolio()``'s avg-cost total by more than
        ``QUANTITY_EPSILON`` shares — never a bare ``==``. Logs a warning
        per mismatch; never raises.

        Story 2.3: FIFO's ``open_lots`` structurally can never go negative
        — an oversell's shortfall is recorded separately, in
        ``unmatched_sells``, never merged back into ``open_lots``. Since
        the average-cost clamp was removed, its ``shares`` figure now goes
        negative for the identical, legitimately-oversold ticker. Comparing
        bare ``open_lots`` totals against avg-cost's negative-capable
        ``shares`` would therefore read as a large false divergence for
        every oversold ticker. Netting each ticker's ``unmatched_sells``
        shortfall against its ``open_lots`` total first produces a FIFO-side
        net position directly comparable to avg-cost's ``shares``, including
        its negative range, before the two are compared.
        """
        fifo_shares = {
            ticker: sum(lot.shares_remaining for lot in lots)
            for ticker, lots in open_lots.items()
        }
        shortfalls: dict[str, float] = {}
        for unmatched in unmatched_sells:
            shortfalls[unmatched.ticker] = (
                shortfalls.get(unmatched.ticker, 0.0) + unmatched.shares
            )
        avg_cost_positions = self._trader.get_portfolio(portfolio_id=portfolio_id)
        avg_cost_shares = {p.ticker: p.shares for p in avg_cost_positions}
        mismatched: list[str] = []
        all_tickers = set(fifo_shares) | set(avg_cost_shares) | set(shortfalls)
        for ticker in sorted(all_tickers):
            fifo_net = round_quantity(
                fifo_shares.get(ticker, 0.0) - shortfalls.get(ticker, 0.0)
            )
            avg_total = round_quantity(avg_cost_shares.get(ticker, 0.0))
            if abs(fifo_net - avg_total) > QUANTITY_EPSILON:
                mismatched.append(ticker)
                logger.warning(
                    "Realised P&L FIFO/avg-cost mismatch for %s: "
                    "fifo_net_shares=%.8f avg_cost_shares=%.8f",
                    ticker,
                    fifo_net,
                    avg_total,
                )
        return mismatched
