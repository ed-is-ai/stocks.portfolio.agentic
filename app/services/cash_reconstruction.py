"""Reconstruct a portfolio's dated cash balance from anchors + deltas (#543).

Before this module the snapshot backfill took each day's cash straight from
the last *stated* statement balance on or before it (``balances_as_of``).
That is right at a statement date and wrong everywhere else: a BUY or SELL
between two statements moves value out of (or into) cash, but the carried
balance never moved, so the Portfolio Value line -- market value *plus*
cash -- stepped down on every sale and up on every purchase. Worse, a day
before the first statement had no balance to carry at all, so every such row
was written with ``NULL`` cash, which forced the whole chart onto its
Market-Value-only fallback.

The fix is anchor-plus-delta. Each stated balance is an anchor pinned to its
own date; any other day is that anchor plus everything that moved cash in
between -- signed ``cash_flows`` and signed trade proceeds -- rolled forward
when the day is after the anchor and unwound backward when it is before.
The *nearest* anchor in either direction is used. Between two statements
that bounds the drift from any unattributable flow to a single gap; outside
them -- before the first statement, after the last -- the interval is the
whole distance to the one anchor there is, so accuracy degrades the further
a day sits from a stated balance. Every statement import narrows that.

Nothing here fabricates a figure: with no anchor at all the answer is
``None``, and the caller folds currencies to GBP only through dated FX
evidence, returning ``None`` rather than a partial total.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.services.snapshot_repair import net_trade_cash

#: Direction of each ``cash_flows.flow_type``. The stored ``amount`` is
#: magnitude-only (``CHECK(amount > 0)``) and the provider's Debit/Credit
#: column is discarded at import -- only the type classified by
#: ``classify_flow_type`` (``trader_agent.py``) survives, so direction has to
#: be recovered from it.
#:
#: ``TRANSFER`` and ``OTHER`` are genuinely directionless (a transfer can go
#: either way; ``OTHER`` is the classifier's catch-all) and ``OPENING`` is a
#: stated balance rather than a movement, so all three contribute zero rather
#: than a guess -- a wrong cash figure silently shifts the Portfolio Value
#: line, which is the very defect this module exists to fix.
#:
#: ponytail: unattributed flows distort the reconstruction within a single
#: inter-statement gap; the upgrade path is persisting the signed amount at
#: import time instead of re-deriving direction from the type here.
FLOW_SIGNS: dict[str, int] = {
    "CONTRIBUTION": 1,
    "DIVIDEND": 1,
    "INTEREST": 1,
    "TAX_RELIEF": 1,
    "WITHDRAWAL": -1,
    "TRANSFER": 0,
    "OTHER": 0,
    "OPENING": 0,
}

#: Trades settle in the account's base currency, and replay prices are
#: already GBP major units, so trade deltas apply to this currency alone.
_TRADE_CURRENCY = "GBP"


class CashReconstruction:
    """Answers "what was the cash balance on this day?" per currency (#543).

    Built once per portfolio from its dated statement anchors, its whole
    ``cash_flows`` ledger and its trade replay rows, then queried for every
    day the backfill writes -- the ledgers are scanned per query but never
    re-read from the database.
    """

    def __init__(
        self,
        anchors: list[tuple[str, str, Decimal]],
        flows: list[tuple[str, str, float, str]],
        replay_rows: list[tuple[Any, ...]],
    ) -> None:
        """Store the evidence three reconstructions are built from.

        ``anchors`` are ``(as_of, currency, amount)`` stated balances,
        ``flows`` are ``(date, flow_type, amount, currency)`` movements, and
        ``replay_rows`` are the shared ``(ticker, action, shares, price,
        date, ...)`` trade tuples.
        """
        self._anchors: dict[str, list[tuple[str, Decimal]]] = {}
        for as_of, currency, amount in anchors:
            self._anchors.setdefault(currency, []).append((as_of[:10], amount))
        self._flows = flows
        self._replay_rows = replay_rows

    def balances_at(self, as_of: str) -> dict[str, Decimal] | None:
        """Return ``{currency: balance}`` on ``as_of``, or None with no anchor.

        Each currency is answered from its own nearest anchor by absolute
        date distance -- ties resolve to the earlier anchor, so an
        equidistant day rolls forward rather than backward. ``None`` means
        the portfolio has no stated balance whatsoever, which is the one
        case where any figure would be invented; it leaves the day's cash
        ``NULL`` and the chart on its Market-Value fallback, exactly as
        before.

        A currency reconstructing to zero is omitted rather than reported.
        Every anchored currency is projected across the *whole* series, so a
        USD balance first stated this year is also asked about years before
        the account held any -- where it unwinds to nothing. Reporting that
        zero would be a phantom holding, and worse: the caller returns
        ``None`` for the entire day when any currency lacks a dated FX rate,
        so one recent foreign anchor would otherwise blank the cash on every
        historical row and put the chart straight back on the fallback this
        change exists to retire.
        """
        if not self._anchors:
            return None
        day = as_of[:10]
        balances = {
            currency: self._balance_for(currency, day) for currency in self._anchors
        }
        return {currency: amount for currency, amount in balances.items() if amount}

    def _balance_for(self, currency: str, day: str) -> Decimal:
        """Return one currency's balance on ``day`` from its nearest anchor."""
        anchor_day, amount = self._nearest_anchor(currency, day)
        if day >= anchor_day:
            return amount + self._delta(currency, anchor_day, day)
        # Rolling backward: the anchor already includes everything that moved
        # in ``(day, anchor_day]``, so unwinding it means subtracting it.
        return amount - self._delta(currency, day, anchor_day)

    def _nearest_anchor(self, currency: str, day: str) -> tuple[str, Decimal]:
        """Return the ``(as_of, amount)`` anchor closest to ``day`` in time."""
        target = _ordinal(day)
        return min(
            self._anchors[currency],
            key=lambda anchor: (abs(_ordinal(anchor[0]) - target), anchor[0]),
        )

    def _delta(
        self, currency: str, start_exclusive: str, end_inclusive: str
    ) -> Decimal:
        """Return the signed cash movement in ``(start, end]`` for ``currency``."""
        total = Decimal("0")
        for flow_date, flow_type, amount, flow_currency in self._flows:
            if flow_currency != currency:
                continue
            if not (start_exclusive < flow_date[:10] <= end_inclusive):
                continue
            sign = FLOW_SIGNS.get(flow_type.strip().upper(), 0)
            if sign:
                total += Decimal(str(amount)) * sign
        if currency == _TRADE_CURRENCY:
            # Trade rows are written to ``planned_trades`` only, never to
            # ``cash_flows`` (see the SIPP import), so adding them here
            # cannot double-count what the loop above already added.
            total += Decimal(
                str(net_trade_cash(self._replay_rows, start_exclusive, end_inclusive))
            )
        return total


def _ordinal(day: str) -> int:
    """Return the proleptic day number for ``YYYY-MM-DD``, 0 if unparseable.

    Only ever used to measure the *distance* between two dates, so a
    malformed anchor date simply loses the nearest-anchor comparison to any
    well-formed one instead of raising mid-backfill.
    """
    try:
        return date.fromisoformat(day).toordinal()
    except ValueError:
        return 0
