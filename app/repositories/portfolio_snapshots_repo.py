"""Repository for the per-portfolio ``portfolio_snapshots`` table (#147).

Replaces the single ``portfolio_value.csv`` value log: each portfolio keeps
its own value-history series so the Portfolio tab's chart is scoped to the
selected account.
"""

from typing import Any

from app.repositories.db import Connect, session


class PortfolioSnapshotsRepository:
    """Typed access to the ``portfolio_snapshots`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def append(
        self,
        portfolio_id: int,
        timestamp: str,
        total_value: float,
        total_cost: float,
        cash_balance: float | None,
    ) -> None:
        """Append one value snapshot for a portfolio."""
        with session(self._connect) as conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(portfolio_id, timestamp, total_value, total_cost, cash_balance) "
                "VALUES (?, ?, ?, ?, ?)",
                (portfolio_id, timestamp, total_value, total_cost, cash_balance),
            )

    def history(self, portfolio_id: int, limit: int = 180) -> list[tuple[Any, ...]]:
        """Return the most recent ``limit`` snapshots oldest-first.

        Columns: ``(timestamp, total_value, total_cost, cash_balance)``.
        """
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT timestamp, total_value, total_cost, cash_balance "
                "FROM portfolio_snapshots WHERE portfolio_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (portfolio_id, limit),
            ).fetchall()
        return list(reversed(rows))
