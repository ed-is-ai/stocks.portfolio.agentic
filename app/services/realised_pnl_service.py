"""Realised P&L service — FIFO lot matching and round-trip shaping.

Epic 1, Story 1.1. Owns FIFO matching only: currency conversion (Story 1.2),
unmatched-sell shaping (Story 1.3), and acknowledgment (Story 1.5) are later
stories layered on top without changing this story's schema fields.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import date

from app.schemas import RealisedPnlSummary, RoundTrip, Trade
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)

# Share-count epsilon for the FIFO-vs-avg-cost mismatch check (AD-10). Never
# compare floats with a bare `==`.
_EPSILON = 1e-6
_INVALID_TICKERS = {"", "n/a", "N/A"}


@dataclass
class _Lot:
    """One open BUY lot in a per-ticker FIFO queue."""

    shares_remaining: float
    buy_price: float
    buy_date: str


class RealisedPnlService:
    """FIFO-matches SELLs against the oldest open BUY lots per ticker.

    Reads trade rows via ``TraderService.get_trade_history()``, which
    returns newest-first, then sorts them ascending by ``(date, id)`` and
    drops non-positive-share / invalid-ticker rows itself (AD-4) — this is
    the same ordering/filter ``TradesRepository.open_rows()`` applies for
    the average-cost replay, reimplemented independently here. This service
    must never call or import the Portfolio tab's average-cost matching
    method on ``TraderAgent`` (it stays untouched).

    No persistence or caching of any kind: every ``compute_summary`` call
    recomputes fresh from live trade data.
    """

    def __init__(self, trader_service: TraderService) -> None:
        self._trader = trader_service

    def compute_summary(self, portfolio_id: int) -> RealisedPnlSummary:
        """Return a fully-populated Realised P&L summary for one Account.

        Recomputed from scratch on every call — nothing is cached or
        persisted (AC 6).
        """
        trades = self._sorted_valid_trades(portfolio_id)
        round_trips, open_lots, unmatched_count = self._replay_fifo(
            trades, portfolio_id
        )
        grouped = self._group_and_order(round_trips)
        mismatched = self._mismatched_tickers(open_lots, portfolio_id)
        total_pnl = round(sum(rt.realised_pnl_gbp for rt in round_trips), 2)
        return RealisedPnlSummary(
            portfolio_id=portfolio_id,
            round_trips=grouped,
            total_realised_pnl_gbp=total_pnl,
            round_trip_count=len(round_trips),
            unmatched_count=unmatched_count,
            mismatched_tickers=mismatched,
        )

    def _sorted_valid_trades(self, portfolio_id: int) -> list[Trade]:
        """Fetch, filter, and sort trades for FIFO replay.

        The only place trade rows are fetched/sorted for FIFO purposes.
        Must never call or import the average-cost replay method on
        ``TraderAgent``. A row whose ``date`` isn't valid ISO-8601 is
        skipped defensively (logged, not raised) — same tolerance
        philosophy as the shares/ticker filter below; this codebase has
        shipped and fixed real trade-date corruption before (#166, #167),
        so a malformed date reaching this far, while unexpected, must not
        crash the whole Account's Realised P&L computation.
        """
        trades = self._trader.get_trade_history(portfolio_id=portfolio_id)
        valid = []
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
                continue
            valid.append(t)
        return sorted(valid, key=lambda t: (t.date, t.id or 0))

    def _replay_fifo(
        self, trades: list[Trade], portfolio_id: int
    ) -> tuple[list[RoundTrip], dict[str, deque[_Lot]], int]:
        """Replay trades in order, FIFO-matching SELLs against open lots.

        On BUY, push a new lot onto that ticker's queue. On SELL, pop from
        the front, consuming up to ``shares_remaining`` per lot until the
        sold quantity is satisfied or the queue empties; a fully-consumed
        lot is dequeued immediately so it can never be re-matched. If the
        queue empties before a SELL is fully satisfied, matching for that
        SELL simply stops — no exception, no fabricated lot, and the
        shortfall is only counted (``unmatched_count``), never shaped into
        a return value (Story 1.3's job).
        """
        queues: dict[str, deque[_Lot]] = {}
        round_trips: list[RoundTrip] = []
        unmatched_count = 0
        for t in trades:
            queue = queues.setdefault(t.ticker, deque())
            if t.action == "BUY":
                queue.append(_Lot(t.shares, t.price, t.date))
                continue
            remaining_to_sell = t.shares
            while remaining_to_sell > _EPSILON and queue:
                lot = queue[0]
                matched = min(lot.shares_remaining, remaining_to_sell)
                round_trips.append(
                    self._build_round_trip(t, lot, matched, portfolio_id)
                )
                lot.shares_remaining -= matched
                remaining_to_sell -= matched
                if lot.shares_remaining <= _EPSILON:
                    queue.popleft()
            if remaining_to_sell > _EPSILON:
                unmatched_count += 1
        return round_trips, queues, unmatched_count

    @staticmethod
    def _build_round_trip(
        sell: Trade, lot: _Lot, matched_shares: float, portfolio_id: int
    ) -> RoundTrip:
        """Build one RoundTrip for a lot (or partial lot) consumed by a SELL.

        ``realised_pnl_gbp``/``realised_pnl_pct`` are native-currency
        placeholders in this story (the trade's raw price is treated as if
        already GBP); Story 1.2 replaces the calculation with true
        trade-date FX conversion without changing these field names.
        """
        entry_price = lot.buy_price
        exit_price = sell.price
        pnl_gbp = round((exit_price - entry_price) * matched_shares, 2)
        cost_basis = entry_price * matched_shares
        pnl_pct = round(pnl_gbp / cost_basis * 100, 2) if cost_basis > 0 else 0.0
        holding_days = (
            date.fromisoformat(sell.date) - date.fromisoformat(lot.buy_date)
        ).days
        return RoundTrip(
            ticker=sell.ticker,
            portfolio_id=portfolio_id,
            entry_date=lot.buy_date,
            entry_price=entry_price,
            exit_date=sell.date,
            exit_price=exit_price,
            shares=matched_shares,
            holding_period_days=holding_days,
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
        self, open_lots: dict[str, deque[_Lot]], portfolio_id: int
    ) -> list[str]:
        """Cross-check FIFO open-lot totals against the avg-cost replay.

        Per AD-10, a ticker is reported only when its FIFO open-lot share
        total disagrees with ``TraderService.get_portfolio()``'s avg-cost
        total by more than ``_EPSILON`` shares — never a bare ``==``. Logs
        a warning per mismatch; never raises.
        """
        fifo_shares = {
            ticker: sum(lot.shares_remaining for lot in lots)
            for ticker, lots in open_lots.items()
        }
        avg_cost_positions = self._trader.get_portfolio(portfolio_id=portfolio_id)
        avg_cost_shares = {p.ticker: p.shares for p in avg_cost_positions}
        mismatched: list[str] = []
        for ticker in sorted(set(fifo_shares) | set(avg_cost_shares)):
            fifo_total = fifo_shares.get(ticker, 0.0)
            avg_total = avg_cost_shares.get(ticker, 0.0)
            if abs(fifo_total - avg_total) > _EPSILON:
                mismatched.append(ticker)
                logger.warning(
                    "Realised P&L FIFO/avg-cost mismatch for %s: "
                    "fifo_shares=%.6f avg_cost_shares=%.6f",
                    ticker,
                    fifo_total,
                    avg_total,
                )
        return mismatched
