"""Repository for the ``cash_flows`` table in ``trades.db``."""

from typing import Any

from app.repositories.db import Connect, session
from app.schemas.trade import CashFlow


def _row_to_cash_flow(row: tuple[Any, ...]) -> CashFlow:
    return CashFlow(
        id=row[0],
        date=row[1],
        flow_type=row[2],
        ticker=row[3],
        amount=row[4],
        description=row[5],
        reference=row[6],
        portfolio_id=row[7],
    )


class CashFlowsRepository:
    """Typed access to the ``cash_flows`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def history(
        self, portfolio_id: int | None = None, limit: int = 200
    ) -> list[CashFlow]:
        """Return cash flows newest-first, optionally scoped to a portfolio."""
        base = (
            "SELECT id, date, flow_type, ticker, amount, description, reference,"
            " portfolio_id FROM cash_flows"
        )
        params: tuple[Any, ...] = ()
        if portfolio_id is not None:
            base += " WHERE portfolio_id = ?"
            params = (portfolio_id,)
        base += " ORDER BY date DESC, id DESC LIMIT ?"
        params = (*params, limit)
        with session(self._connect) as conn:
            rows = conn.execute(base, params).fetchall()
        return [_row_to_cash_flow(r) for r in rows]

    def insert_ignore(
        self,
        conn: Any,
        date: str,
        flow_type: str,
        ticker: str | None,
        amount: float,
        description: str | None,
        reference: str | None,
        portfolio_id: int | None = None,
    ) -> None:
        """Insert a cash flow with ``INSERT OR IGNORE`` on the given connection.

        The ``(portfolio_id, reference)`` pair makes the SIPP import idempotent
        per-portfolio. Inserts share the import's connection so the whole import
        is one transaction.
        """
        conn.execute(
            "INSERT OR IGNORE INTO cash_flows "
            "(date, flow_type, ticker, amount, description, reference, portfolio_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, flow_type, ticker, amount, description, reference, portfolio_id),
        )
