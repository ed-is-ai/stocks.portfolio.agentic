"""Repository for the ``cash_balance_history`` table in ``trades.db`` (#514).

``cash_balances`` records only each currency's *current* winning Running
Balance. This table keeps the whole dated series the provider stated -- one
row per (portfolio, currency, date) -- so the snapshot backfill can ask
"what was the cash balance on 2022-06-14?" and get the account's own figure
rather than a total reconstructed from ``cash_flows`` deltas, which cannot be
trusted (positive-only amounts, and a heterogeneous ``OTHER`` type that says
nothing about direction).

Written on the import's open connection so it joins the same all-or-nothing
transaction as the trade/cash-flow/snapshot writes.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.repositories.db import Connect, session


class CashBalanceHistoryRepository:
    """Typed access to the dated per-currency cash-balance series."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def upsert_on_connection(
        self,
        conn: Any,
        portfolio_id: int,
        currency: str,
        as_of: str,
        amount: Decimal,
        source_reference: str | None = None,
    ) -> None:
        """Record one dated balance on the caller's connection; no commit.

        Re-importing an overlapping file restates the same day rather than
        duplicating it: the primary key is ``(portfolio_id, currency,
        as_of)`` and a conflict overwrites, so the newest import wins for a
        day both files describe.
        """
        conn.execute(
            "INSERT INTO cash_balance_history "
            "(portfolio_id, currency, as_of, amount, source_reference, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(portfolio_id, currency, as_of) DO UPDATE SET "
            "  amount=excluded.amount, "
            "  source_reference=excluded.source_reference, "
            "  updated_at=excluded.updated_at",
            (
                portfolio_id,
                currency,
                as_of,
                str(amount),
                source_reference,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def balances_as_of(self, portfolio_id: int, as_of: str) -> dict[str, Decimal]:
        """Return ``{currency: balance}`` in force on ``as_of``.

        Each currency's most recent stated balance on or before ``as_of`` is
        carried forward, which is what a balance means between statements. A
        currency with no balance recorded on or before that date is absent
        from the result -- never defaulted to zero, since "no statement yet"
        and "an account holding nothing" are different facts.
        """
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT h.currency, h.amount FROM cash_balance_history h "
                "JOIN (SELECT currency, MAX(as_of) AS as_of "
                "        FROM cash_balance_history "
                "       WHERE portfolio_id = ? AND as_of <= ? "
                "       GROUP BY currency) latest "
                "  ON latest.currency = h.currency AND latest.as_of = h.as_of "
                "WHERE h.portfolio_id = ?",
                (portfolio_id, as_of, portfolio_id),
            ).fetchall()
        return {row[0]: Decimal(row[1]) for row in rows}

    def earliest_as_of(self, portfolio_id: int) -> str | None:
        """Return the first date any balance is known for, or None.

        The backfill uses this to tell "before the account's history began"
        (leave the day's cash NULL) apart from "a day the statements cover".
        """
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT MIN(as_of) FROM cash_balance_history WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()
        return None if row is None else row[0]

    def count(self, portfolio_id: int) -> int:
        """Return how many dated balances are stored for a portfolio."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM cash_balance_history WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()
        return int(row[0]) if row else 0
