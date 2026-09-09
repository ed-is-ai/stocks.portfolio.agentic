"""Realised P&L service — FIFO lot matching, round-trip shaping, trade-date
GBP conversion, and unmatched-sell detection.

Epic 1, Story 1.1 (FIFO matching) + Story 1.2 (trade-date FX conversion with
historical rate caching) + Story 1.3 (unmatched-sell detection).
Acknowledgment (Story 1.5) is a later story layered on top without changing
this story's schema fields.
"""

from __future__ import annotations

import logging
from hashlib import sha256
import json
from collections import deque
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from threading import RLock
from typing import Any, Literal

from app.core.quantity import QUANTITY_EPSILON, round_quantity
from app.core.config import TICKER_ALIASES_JSON
from app.core.ticker_identity import (
    canonicalize_or_fallback,
    matching_raw_tickers,
)
from app.schemas import (
    MatchTrace,
    MatchTraceCandidateLot,
    RealisedPnlSummary,
    RoundTrip,
    SkippedInvalidDateTrade,
    Trade,
    UnmatchedSell,
)
from app.services.portfolio_service import CONVERTIBLE_CURRENCIES, PortfolioService
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
_CACHE_LIMIT = 128


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

    FX rate resolution goes exclusively through ``PortfolioService``
    (``historical_fx_rates``, AD-5) — this service never fetches market data
    or the live rate itself. AD-5 also named ``ticker_currencies`` the sole
    currency seam, which held while a ticker had one currency; there are now
    two facts (the currency trades were priced in, and the currency the
    ticker quotes in), so the trading currency comes from the ledger via
    ``TraderService.resolve_trade_currencies`` and ``ticker_currencies``
    keeps the quote question it is named for (#553/#554). The portfolio boundary
    may reuse immutable currency classifications; FIFO and summary data are
    always recomputed from the current trade ledger.

    Derived summaries are cached against durable trade and valuation-input
    revisions plus the current ticker-alias map. Mutation guards deliberately
    remain uncached.
    """

    def __init__(
        self, trader_service: TraderService, portfolio_service: PortfolioService
    ) -> None:
        self._trader = trader_service
        self._portfolio = portfolio_service
        self._cache_lock = RLock()
        self._summary_cache: OrderedDict[
            tuple[int, int, int, str], RealisedPnlSummary
        ] = OrderedDict()
        self._history_cache: OrderedDict[
            tuple[tuple[tuple[int, int], ...], str],
            tuple[list[Trade], dict[int, Literal["unconsumed", "consumed"]]],
        ] = OrderedDict()

    def compute_summary(self, portfolio_id: int) -> RealisedPnlSummary:
        """Return a revision-validated, independently mutable P&L summary."""
        try:
            trade_revision = self._trader.get_trade_revision(portfolio_id)
            input_revision = self._trader.get_pnl_input_revision()
            alias_identity = self._alias_identity()
        except Exception:
            logger.warning("Realised P&L: revision unavailable; bypassing cache")
            return self._compute_summary_uncached(portfolio_id)
        key = (portfolio_id, trade_revision, input_revision, alias_identity)
        cached = self._cache_get(self._summary_cache, key)
        if cached is not None:
            return cached.model_copy(deep=True)

        # Do not seed a cache entry from a ledger snapshot that was changed
        # while FIFO/FX work was underway. A bounded retry handles a write
        # racing the first computation; the final fallback stays uncached.
        for _ in range(2):
            summary = self._compute_summary_uncached(portfolio_id)
            try:
                current_trade_revision = self._trader.get_trade_revision(portfolio_id)
                current_input_revision = self._trader.get_pnl_input_revision()
                current_alias_identity = self._alias_identity()
            except Exception:
                return summary
            if (
                current_trade_revision == trade_revision
                and current_input_revision == input_revision
                and current_alias_identity == alias_identity
            ):
                self._cache_put(self._summary_cache, key, summary)
                return summary.model_copy(deep=True)
            trade_revision = current_trade_revision
            input_revision = current_input_revision
            alias_identity = current_alias_identity
            key = (portfolio_id, trade_revision, input_revision, alias_identity)
            cached = self._cache_get(self._summary_cache, key)
            if cached is not None:
                return cached.model_copy(deep=True)
        return self._compute_summary_uncached(portfolio_id)

    def _alias_identity(self) -> str:
        """Return a stable identity for file-backed FIFO alias configuration.

        The effective mapping makes this work with injected/mock portfolio
        services. For the real file-backed source, filesystem change identity
        also prevents an A→B→A edit while FIFO is running from looking stable
        merely because its final aliases happen to equal its initial aliases.
        """
        aliases = self._portfolio.load_ticker_aliases()
        try:
            stat = TICKER_ALIASES_JSON.stat()
            file_identity: tuple[int, int, int, int, int] | None = (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
        except OSError:
            file_identity = None
        payload = json.dumps(
            {"aliases": sorted(aliases.items()), "file": file_identity},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    def _compute_summary_uncached(self, portfolio_id: int) -> RealisedPnlSummary:
        """Build a P&L summary from the current ledger without cache access."""
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
            gross_won_gbp=sum(rt.realised_pnl_gbp for rt in winning_round_trips),
            gross_lost_gbp=sum(rt.realised_pnl_gbp for rt in losing_round_trips),
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

    @staticmethod
    def _history_key(
        revisions: dict[int, int], alias_identity: str
    ) -> tuple[tuple[tuple[int, int], ...], str]:
        return tuple(sorted(revisions.items())), alias_identity

    def get_history_presentation(
        self, portfolio_ids: list[int]
    ) -> tuple[list[Trade], dict[int, Literal["unconsumed", "consumed"]]]:
        """Return cached History rows and display-only Opening Lot statuses.

        Portfolio display names intentionally stay in the route: renaming an
        account must be visible without changing its trade revision.
        """
        ids = list(dict.fromkeys([*portfolio_ids, -1]))
        try:
            revisions = self._trader.get_trade_revisions(ids)
            alias_identity = self._alias_identity()
        except Exception:
            logger.warning("Trade History: revisions unavailable; bypassing cache")
            return self._compute_history_presentation_uncached()
        key = self._history_key(revisions, alias_identity)
        cached = self._cache_get(self._history_cache, key)
        if cached is not None:
            return deepcopy(cached)

        for _ in range(2):
            presentation = self._compute_history_presentation_uncached()
            try:
                current_revisions = self._trader.get_trade_revisions(ids)
                current_alias_identity = self._alias_identity()
            except Exception:
                return presentation
            if (
                current_revisions == revisions
                and current_alias_identity == alias_identity
            ):
                self._cache_put(self._history_cache, key, presentation)
                return deepcopy(presentation)
            revisions = current_revisions
            alias_identity = current_alias_identity
            key = self._history_key(revisions, alias_identity)
            cached = self._cache_get(self._history_cache, key)
            if cached is not None:
                return deepcopy(cached)
        return self._compute_history_presentation_uncached()

    def _compute_history_presentation_uncached(
        self,
    ) -> tuple[list[Trade], dict[int, Literal["unconsumed", "consumed"]]]:
        trades = self._trader.get_trade_history()
        return trades, self.opening_lot_statuses(trades)

    def _cache_get(self, cache: OrderedDict, key: object):
        with self._cache_lock:
            value = cache.get(key)
            if value is not None:
                cache.move_to_end(key)
            return value

    def _cache_put(self, cache: OrderedDict, key: object, value: object) -> None:
        with self._cache_lock:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > _CACHE_LIMIT:
                cache.popitem(last=False)

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

    def opening_lot_statuses(
        self, trades: list[Trade]
    ) -> dict[int, Literal["unconsumed", "consumed"]]:
        """Return FIFO consumption status for every Opening Lot in history.

        ``partial_history`` already loads the all-portfolio trade list for
        rendering, so it supplies that list here instead of causing a second
        global history read. The list is split by relevant portfolio and each
        portfolio is filtered, sorted, and replayed exactly once. This is a
        display-only batch operation; edit/delete guards deliberately retain
        ``opening_lot_status`` for their fresh authoritative check.

        As with the single-lot operation, an invalid-date Opening Lot never
        enters a queue and therefore reads as ``"consumed"``; a partially or
        fully depleted lot does too. Statuses are keyed only by persisted
        trade ID, avoiding ticker spelling collisions after alias resolution.
        """
        opening_lots = [
            trade
            for trade in trades
            if trade.source == "opening_lot"
            and trade.id is not None
            and trade.portfolio_id is not None
        ]
        relevant_portfolio_ids = {
            trade.portfolio_id
            for trade in opening_lots
            if trade.portfolio_id is not None
        }
        statuses: dict[int, Literal["unconsumed", "consumed"]] = {}
        for portfolio_id in relevant_portfolio_ids:
            portfolio_trades = [
                trade for trade in trades if trade.portfolio_id == portfolio_id
            ]
            valid_trades, _ = self._filter_and_sort_trades(portfolio_trades)
            _, open_lots, _, _ = self._replay_fifo(valid_trades, portfolio_id)
            open_lots_by_trade_id = {
                lot.trade_id: lot
                for lots in open_lots.values()
                for lot in lots
                if lot.trade_id is not None
            }
            for trade in opening_lots:
                if trade.portfolio_id != portfolio_id:
                    continue
                # ``opening_lots`` was already filtered on `trade.id is not
                # None` above.
                assert trade.id is not None
                lot = open_lots_by_trade_id.get(trade.id)
                statuses[trade.id] = (
                    "unconsumed"
                    if lot is not None
                    and abs(lot.shares_remaining - trade.shares) <= QUANTITY_EPSILON
                    else "consumed"
                )
        return statuses

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
        return self._filter_and_sort_trades(trades)

    def _filter_and_sort_trades(
        self, trades: list[Trade]
    ) -> tuple[list[Trade], list[SkippedInvalidDateTrade]]:
        """Filter and deterministically sort already-loaded FIFO inputs."""
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

        A Round-trip's legs are FIFO *trade* prices, so they are converted
        from the currency the trades were priced in, not the one the ticker
        quotes in (#554). Those are two different facts about the same
        holding, and using the quote currency divided sterling-priced legs by
        a rate they never traded at. ``TraderService.resolve_trade_currencies``
        answers the first question (#553's evidence-backed resolution);
        ``PortfolioService.ticker_currencies`` still answers the second, and
        remains the fallback for a ticker the ledger cannot resolve -- an
        alias whose canonical spelling has no trade rows of its own -- so an
        unresolvable holding keeps its previous behaviour rather than
        silently changing basis.

        Distinct trade dates are then batch-fetched once per currency, over
        whatever :data:`~app.services.portfolio_service.CONVERTIBLE_CURRENCIES`
        holds rather than a set restated here; the restated copy is what
        dropped every EUR Round-trip when the shared one grew (#554). A
        GBP-priced ticker never needs a rate lookup.
        """
        tickers = list(dict.fromkeys(raw.ticker for raw in raw_round_trips))
        currencies = self._evidenced_trade_currencies(tickers)
        unresolved = [ticker for ticker in tickers if ticker not in currencies]
        if unresolved:
            logger.info(
                "Realised P&L: no trade currency for %s -- falling back to the "
                "quote currency",
                ", ".join(sorted(unresolved)),
            )
            currencies = {**self._portfolio.ticker_currencies(unresolved), **currencies}

        dates_by_currency: dict[str, set[str]] = {}
        for raw in raw_round_trips:
            currency = currencies.get(raw.ticker, "")
            if currency in CONVERTIBLE_CURRENCIES:
                dates = dates_by_currency.setdefault(currency, set())
                dates.add(raw.entry_date)
                dates.add(raw.exit_date)
        rates_by_currency = {
            currency: self._portfolio.historical_fx_rates(currency, sorted(dates))
            for currency, dates in dates_by_currency.items()
        }

        return [
            self._convert_round_trip(
                raw,
                currencies.get(raw.ticker, ""),
                rates_by_currency.get(currencies.get(raw.ticker, ""), {}),
            )
            for raw in raw_round_trips
        ]

    def _evidenced_trade_currencies(self, tickers: list[str]) -> dict[str, str]:
        """Return ``{canonical ticker: evidenced trading currency}``.

        ``tickers`` are canonical identities (``SGLN.L``); the trades table
        holds whatever spelling was imported (``SGLN``), so the lookup goes
        through ``matching_raw_tickers`` and the answers fold back onto the
        canonical identity. Asking by the canonical name alone matched
        nothing for every aliased ticker -- most of the foreign ones, and
        both holdings #554 exists to fix.

        Only evidenced verdicts (#553) are returned, never the ledger's
        lower tiers. Those end in a bare ``'GBP'`` default, and treating
        that default as an answer is worse than the quote currency it would
        displace: the euro holdings have no verdict, and taking their
        default would have valued €-priced legs as sterling. An absent
        ticker means "the ledger does not know", and the caller falls back.

        Where two spellings of one holding carry different verdicts the
        first sorted wins -- arbitrary but stable, and a disagreement means
        the ledger holds two currencies for one security, which is a data
        problem this cannot paper over.
        """
        aliases = self._portfolio.load_ticker_aliases()
        spellings = {
            ticker: sorted(matching_raw_tickers(ticker, aliases)) for ticker in tickers
        }
        evidenced = self._trader.evidenced_trade_currencies(
            sorted({s for group in spellings.values() for s in group})
        )
        resolved = {}
        for ticker, group in spellings.items():
            found = [evidenced[s] for s in group if s in evidenced]
            if found:
                resolved[ticker] = found[0]
        return resolved

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
        elif currency in CONVERTIBLE_CURRENCIES:
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
    def timeline_points(summary: RealisedPnlSummary) -> list[dict[str, Any]]:
        """Project a summary's Round-trips into chart-ready points (#563).

        One point per closed Round-trip, anchored at its ``exit_date``,
        because that is the day a result is realised. Oldest first, so the
        chart's own ordering never depends on AD-9's most-recent-first group
        order.

        ``fx_unavailable`` Round-trips are excluded rather than plotted:
        their P&L fields hold a documented ``0.0`` placeholder, and a dot at
        zero would read as a trade that broke even. They are already absent
        from ``total_realised_pnl_gbp`` for the same reason.

        ``stake`` is the money that was actually committed
        (``entry_price * shares``), so the chart can size a point by what was
        at risk -- a GBP 7,000 position and a GBP 200 one are not the same
        event even when they return the same percentage.
        """
        points = [
            {
                "t": trip.ticker,
                "x": trip.exit_date,
                "e": trip.entry_date,
                "p": round(trip.realised_pnl_gbp, 2),
                "pc": round(trip.realised_pnl_pct, 2),
                "d": trip.holding_period_days,
                "r": round(trip.entry_price * trip.shares, 2),
            }
            for trips in summary.round_trips.values()
            for trip in trips
            if not trip.fx_unavailable
        ]
        points.sort(key=lambda point: (point["x"], point["t"]))
        return points

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
