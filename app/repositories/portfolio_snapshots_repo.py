"""Repository for the per-portfolio ``portfolio_snapshots`` table (#147).

Replaces the single ``portfolio_value.csv`` value log: each portfolio keeps
its own value-history series so the Portfolio tab's chart is scoped to the
selected account.
"""

from typing import Any

from app.repositories.db import Connect, session

# When ``since`` is given the query is a time window, not a count window, so
# the default 180 cap must not apply. This ceiling only guards against
# pathological data volumes (#421).
_SINCE_SAFETY_CEILING = 20000


class PortfolioSnapshotsRepository:
    """Typed access to the ``portfolio_snapshots`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def append(
        self,
        portfolio_id: int,
        timestamp: str,
        total_value: float | None,
        total_cost: float | None,
        cash_balance: float | None,
    ) -> None:
        """Append one value snapshot for a portfolio.

        ``total_value``/``total_cost`` are None when the holdings could not be
        valued at this timestamp -- stored as SQL NULL so the chart shows an
        honest gap instead of a fabricated 0.00 (#466).
        """
        with session(self._connect) as conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(portfolio_id, timestamp, total_value, total_cost, cash_balance) "
                "VALUES (?, ?, ?, ?, ?)",
                (portfolio_id, timestamp, total_value, total_cost, cash_balance),
            )

    def append_on_connection(
        self,
        conn: Any,
        portfolio_id: int,
        timestamp: str,
        total_value: float | None,
        total_cost: float | None,
        cash_balance: float | None,
    ) -> None:
        """Append one value snapshot on the caller's open connection.

        Takes the caller's open connection and does not commit — used by the
        SIPP import so the snapshot append joins the same transaction as its
        trade/cash-flow writes (AC #1).
        """
        conn.execute(
            "INSERT INTO portfolio_snapshots "
            "(portfolio_id, timestamp, total_value, total_cost, cash_balance) "
            "VALUES (?, ?, ?, ?, ?)",
            (portfolio_id, timestamp, total_value, total_cost, cash_balance),
        )

    def history(
        self, portfolio_id: int, limit: int = 180, since: str | None = None
    ) -> list[tuple[Any, ...]]:
        """Return snapshots oldest-first, optionally windowed by ``since``.

        Columns: ``(timestamp, total_value, total_cost, cash_balance)``. When
        ``since`` is given (an ISO timestamp) the result is a *time* window:
        only snapshots at or after it are returned and the ``limit`` count cap
        is replaced by a high safety ceiling so a wide range is never silently
        truncated to the newest few rows. When ``since`` is omitted the legacy
        ``ORDER BY id DESC LIMIT ?`` behaviour is byte-identical (#421).
        """
        query = (
            "SELECT timestamp, total_value, total_cost, cash_balance "
            "FROM portfolio_snapshots WHERE portfolio_id = ? "
        )
        params: list[Any] = [portfolio_id]
        if since is not None:
            query += "AND timestamp >= ? "
            params.append(since)
        effective_limit = limit if since is None else _SINCE_SAFETY_CEILING
        query += "ORDER BY id DESC LIMIT ?"
        params.append(effective_limit)
        with session(self._connect) as conn:
            rows = conn.execute(query, params).fetchall()
        return list(reversed(rows))

    def rows_with_ids(self, portfolio_id: int | None = None) -> list[tuple[Any, ...]]:
        """Return ``(id, portfolio_id, timestamp, total_value, total_cost)``.

        Oldest-first over every stored snapshot (optionally scoped to one
        portfolio). Used by the historical repair pass, which needs each row's
        identity to update it in place.
        """
        query = (
            "SELECT id, portfolio_id, timestamp, total_value, total_cost "
            "FROM portfolio_snapshots"
        )
        params: list[Any] = []
        if portfolio_id is not None:
            query += " WHERE portfolio_id = ?"
            params.append(portfolio_id)
        query += " ORDER BY id ASC"
        with session(self._connect) as conn:
            return list(conn.execute(query, params).fetchall())

    def update_valuation(
        self, row_id: int, total_value: float | None, total_cost: float | None
    ) -> None:
        """Overwrite one snapshot's valuation columns, leaving the rest alone."""
        with session(self._connect) as conn:
            conn.execute(
                "UPDATE portfolio_snapshots SET total_value = ?, total_cost = ? "
                "WHERE id = ?",
                (total_value, total_cost, row_id),
            )
