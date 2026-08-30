"""Repository for the ``portfolios`` table in ``trades.db`` (#147).

A portfolio is an account (e.g. SIPP, ISA) that scopes its own trades, cash
flows, cash balance, and value-history snapshots. Names may duplicate; the
``id`` is the stable identity threaded through every trade path.
"""

from datetime import datetime, timezone
from typing import Any

from app.repositories.db import Connect, session
from app.schemas.trade import Portfolio


def _row_to_portfolio(row: tuple[Any, ...]) -> Portfolio:
    return Portfolio(id=row[0], name=row[1], created_at=row[2])


class PortfoliosRepository:
    """Typed CRUD access to the ``portfolios`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def create(self, name: str) -> Portfolio:
        """Insert a portfolio and return it (with its new id)."""
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with session(self._connect) as conn:
            cur = conn.execute(
                "INSERT INTO portfolios (name, created_at) VALUES (?, ?)",
                (name, created_at),
            )
            pid = int(cur.lastrowid)  # type: ignore[arg-type]
        return Portfolio(id=pid, name=name, created_at=created_at)

    def list_all(self) -> list[Portfolio]:
        """Return every portfolio, oldest first (creation order)."""
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT id, name, created_at FROM portfolios ORDER BY id"
            ).fetchall()
        return [_row_to_portfolio(r) for r in rows]

    def get(self, portfolio_id: int) -> Portfolio | None:
        """Return the portfolio with ``portfolio_id``, or None if absent."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT id, name, created_at FROM portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()
        return _row_to_portfolio(row) if row else None

    def exists(self, portfolio_id: int) -> bool:
        """Return True if a portfolio with ``portfolio_id`` exists."""
        with session(self._connect) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM portfolios WHERE id = ?", (portfolio_id,)
                ).fetchone()
                is not None
            )

    def rename(self, portfolio_id: int, name: str) -> bool:
        """Rename a portfolio. Returns True if a row was updated."""
        with session(self._connect) as conn:
            cur = conn.execute(
                "UPDATE portfolios SET name = ? WHERE id = ?", (name, portfolio_id)
            )
            return cur.rowcount > 0

    def counts(self, portfolio_id: int) -> tuple[int, int]:
        """Return ``(trade_count, cash_flow_count)`` for a portfolio."""
        with session(self._connect) as conn:
            trades = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE portfolio_id = ?", (portfolio_id,)
            ).fetchone()[0]
            flows = conn.execute(
                "SELECT COUNT(*) FROM cash_flows WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()[0]
        return int(trades), int(flows)

    def delete(self, portfolio_id: int) -> bool:
        """Hard-delete a portfolio and all of its trades, cash flows, and
        snapshots. Returns True if the portfolio existed."""
        with session(self._connect) as conn:
            conn.execute("DELETE FROM trades WHERE portfolio_id = ?", (portfolio_id,))
            conn.execute(
                "DELETE FROM cash_flows WHERE portfolio_id = ?", (portfolio_id,)
            )
            conn.execute(
                "DELETE FROM portfolio_snapshots WHERE portfolio_id = ?",
                (portfolio_id,),
            )
            conn.execute(
                "DELETE FROM portfolio_strategies WHERE portfolio_id = ?",
                (portfolio_id,),
            )
            conn.execute(
                "DELETE FROM account_state WHERE key IN (?, ?)",
                (f"cash_balance:{portfolio_id}", f"cash_balance_date:{portfolio_id}"),
            )
            cur = conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))
            return cur.rowcount > 0
