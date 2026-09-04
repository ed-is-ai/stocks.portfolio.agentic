"""Idempotent repair of zero-valued historical portfolio snapshots (#466).

Before #466 a snapshot whose holdings could not be priced was persisted as a
plausible-looking ``total_value = 0.00``, which the value-history chart drew
as a real crash to zero. This pass finds those rows and either reconstructs
them from *dated historical evidence* or -- far more usually -- rewrites them
as ``NULL``, an honest gap.

It never uses current prices, never guesses an FX rate, and never deletes a
row. Only stored zeros whose portfolio actually held something at that
timestamp are candidates: a cash-only portfolio's ``0.00`` is correct and is
left byte-identical.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from app.core.quantity import QUANTITY_EPSILON
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.trades_repo import TradesRepository

logger = logging.getLogger(__name__)

#: A stored ``total_value`` at or below this magnitude is treated as the
#: defective "zero" the bug wrote, not as a meaningful valuation.
_ZERO_TOLERANCE = 0.005


class HistoricalGbpPriceSource(Protocol):
    """Supplies a dated, GBP-denominated close for one holding.

    Implementations must return None whenever they have no *evidence* for
    ``ticker`` on ``as_of`` -- an approximation, a nearby date, or a
    current price is never an acceptable substitute.
    """

    def gbp_price(self, ticker: str, as_of: str) -> float | None:
        """Return the GBP close for ``ticker`` on ``as_of`` (YYYY-MM-DD)."""
        ...


class NoHistoricalPriceSource:
    """The shipped default: no historical evidence for anything.

    The app stores no per-ticker historical closes for portfolio holdings
    (the ``historical_price_*`` tables are keyed by opaque backtest
    ``security_id``), so every candidate row honestly becomes a gap. The
    protocol seam exists so a real evidence source can be injected later
    without touching the repair logic.
    """

    def gbp_price(self, ticker: str, as_of: str) -> float | None:
        """Return None -- this source has no historical evidence."""
        return None


class SnapshotRepairReport(BaseModel):
    """Counts of what one repair pass did (or, in a dry run, would do)."""

    model_config = ConfigDict(frozen=True)

    scanned: int
    candidates: int
    repaired: int
    marked_unavailable: int
    unchanged: int
    dry_run: bool


class SnapshotRepairService:
    """Repairs zero-valued ``portfolio_snapshots`` rows, idempotently."""

    def __init__(
        self,
        trades: TradesRepository,
        snapshots: PortfolioSnapshotsRepository,
        price_source: HistoricalGbpPriceSource | None = None,
    ) -> None:
        self._trades = trades
        self._snapshots = snapshots
        self._price_source: HistoricalGbpPriceSource = (
            price_source or NoHistoricalPriceSource()
        )

    def repair(
        self, portfolio_id: int | None = None, dry_run: bool = False
    ) -> SnapshotRepairReport:
        """Repair stored-zero snapshots, returning what changed.

        Scoped to ``portfolio_id`` when given. With ``dry_run`` the counts are
        computed exactly as they would be applied, but nothing is written.
        Running the pass a second time reports every row as ``unchanged``.
        """
        rows = self._snapshots.rows_with_ids(portfolio_id)
        replay_cache: dict[int | None, list[tuple[Any, ...]]] = {}
        repaired = marked = unchanged = candidates = 0

        for row in rows:
            row_id, pf_id, timestamp, total_value, total_cost = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
            )
            if not self._is_stored_zero(total_value):
                unchanged += 1
                continue
            if pf_id not in replay_cache:
                replay_cache[pf_id] = self._trades.open_rows(pf_id)
            holdings = self._holdings_as_of(replay_cache[pf_id], str(timestamp)[:10])
            if not holdings:
                # A genuinely empty (cash-only) portfolio: 0.00 is correct.
                unchanged += 1
                continue

            candidates += 1
            value = self._reconstruct(holdings, str(timestamp)[:10])
            if value is None:
                marked += 1
                if not dry_run:
                    self._snapshots.update_valuation(int(row_id), None, total_cost)
                continue
            repaired += 1
            if not dry_run:
                self._snapshots.update_valuation(int(row_id), value, total_cost)

        report = SnapshotRepairReport(
            scanned=len(rows),
            candidates=candidates,
            repaired=repaired,
            marked_unavailable=marked,
            unchanged=unchanged,
            dry_run=dry_run,
        )
        logger.info("snapshot repair: %s", report.model_dump())
        return report

    def _reconstruct(self, holdings: dict[str, float], as_of: str) -> float | None:
        """Return the GBP holdings value at ``as_of``, or None without evidence.

        All-or-nothing: one unpriced holding makes the whole point unavailable.
        A reconstruction that rounds to ``0.00`` is also reported as
        unavailable -- writing it back would recreate the very row this pass
        exists to remove, and would stop a re-run being a no-op.
        """
        total = 0.0
        for ticker, shares in holdings.items():
            price = self._price_source.gbp_price(ticker, as_of)
            if price is None:
                return None
            total += shares * price
        value = round(total, 2)
        return None if value == 0.0 else value

    @staticmethod
    def _is_stored_zero(total_value: Any) -> bool:
        """Return True for the defective ``0.00`` this pass repairs.

        A NULL (already repaired) or any non-zero number is not a candidate.
        """
        if total_value is None:
            return False
        try:
            return abs(float(total_value)) <= _ZERO_TOLERANCE
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _holdings_as_of(
        replay_rows: list[tuple[Any, ...]], as_of: str
    ) -> dict[str, float]:
        """Return ``{ticker: net shares}`` from trades dated on/before ``as_of``.

        Replay columns are ``(ticker, action, shares, price, date, ...)``.
        Tickers whose net position is flat (or short) are omitted.
        """
        net: dict[str, float] = {}
        for row in replay_rows:
            ticker, action, shares, _price, trade_date = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
            )
            if str(trade_date)[:10] > as_of:
                continue
            delta = float(shares) if action == "BUY" else -float(shares)
            net[ticker] = net.get(ticker, 0.0) + delta
        return {t: s for t, s in net.items() if s > QUANTITY_EPSILON}
