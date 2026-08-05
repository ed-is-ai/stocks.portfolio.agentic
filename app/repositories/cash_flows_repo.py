"""Repository for the ``cash_flows`` table in ``trades.db``."""

from typing import Any

from app.repositories.db import Connect


class CashFlowsRepository:
    """Typed access to the ``cash_flows`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

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
