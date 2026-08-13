"""Repository for the ``cash_reconciliation_issues`` table in ``trades.db``."""

from datetime import datetime, timezone
from typing import Any

from app.repositories.db import Connect, session


class CashReconciliationRepository:
    """Typed access to the ``cash_reconciliation_issues`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def insert_issue_on_connection(
        self,
        conn: Any,
        portfolio_id: int | None,
        date: str,
        prior_balance: float,
        expected_balance: float,
        actual_balance: float,
        difference: float,
        row_ref: str | None,
        currency: str = "GBP",
    ) -> None:
        """Record a detected statement-balance discrepancy.

        Takes the caller's open connection and does not commit -- lets the
        SIPP import's reconciliation write join the same transaction as its
        trade/cash-flow writes.
        """
        detected_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        conn.execute(
            "INSERT INTO cash_reconciliation_issues "
            "(portfolio_id, date, prior_balance, expected_balance, actual_balance, "
            "difference, row_ref, currency, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                portfolio_id,
                date,
                prior_balance,
                expected_balance,
                actual_balance,
                difference,
                row_ref,
                currency,
                detected_at,
            ),
        )

    def list_issues(
        self, portfolio_id: int | None, limit: int = 200
    ) -> list[tuple[Any, ...]]:
        """Return recorded issues newest-first, optionally scoped to a
        portfolio. ``portfolio_id=None`` returns no rows -- reconciliation
        is only meaningful for a real portfolio, unlike the legacy
        single-portfolio cash-balance mechanism."""
        if portfolio_id is None:
            return []
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT id, portfolio_id, date, prior_balance, expected_balance, "
                "actual_balance, difference, row_ref, currency, detected_at "
                "FROM cash_reconciliation_issues WHERE portfolio_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (portfolio_id, limit),
            ).fetchall()
        return rows
