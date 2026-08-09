"""Repository for the ``trades`` table in ``trades.db``."""

from typing import Any

from app.repositories.db import Connect, session
from app.schemas import Trade

# Dates are stored ISO (YYYY-MM-DD), which sorts chronologically as text.
_DATE_SORT = "date"
_REPLAY_COLUMNS = "ticker, action, shares, price, date, stop_loss, entry_price"


def _row_to_trade(row: tuple[Any, ...]) -> Trade:
    return Trade(
        id=row[0],
        ticker=row[1],
        action=row[2],
        shares=row[3],
        price=row[4],
        date=row[5],
        notes=row[6],
        stop_loss=row[7] if len(row) > 7 else None,
        entry_price=row[8] if len(row) > 8 else None,
        portfolio_id=row[9] if len(row) > 9 else None,
        realised_pnl_ack_at=row[10] if len(row) > 10 else None,
    )


class TradesRepository:
    """Typed access to the ``trades`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def insert(
        self,
        ticker: str,
        action: str,
        shares: float,
        price: float,
        date: str,
        notes: str = "",
        stop_loss: float | None = None,
        entry_price: float | None = None,
        portfolio_id: int | None = None,
    ) -> int:
        """Insert a trade and return its new id."""
        with session(self._connect) as conn:
            cur = conn.execute(
                "INSERT INTO trades (ticker, action, shares, price, date, notes,"
                " stop_loss, entry_price, portfolio_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker,
                    action,
                    shares,
                    price,
                    date,
                    notes,
                    stop_loss,
                    entry_price,
                    portfolio_id,
                ),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def insert_ignore(
        self,
        conn: Any,
        ticker: str,
        action: str,
        shares: float,
        price: float,
        date: str,
        notes: str = "",
        reference: str | None = None,
        portfolio_id: int | None = None,
    ) -> None:
        """Insert a trade with ``INSERT OR IGNORE`` on the given connection.

        Used by the SIPP import, which batches many rows in one transaction.
        The idempotency key is ``(portfolio_id, reference)`` so the same CSV
        can import into different portfolios independently.
        """
        conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(ticker, action, shares, price, date, notes, reference, portfolio_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, action, shares, price, date, notes, reference, portfolio_id),
        )

    def set_ack(self, conn: Any, trade_id: int, acknowledged_at: str | None) -> None:
        """Set or clear a trade's realised-P&L acknowledgment timestamp.

        ``acknowledged_at`` is an ISO 8601 timestamp string (acknowledged) or
        ``None`` (unacknowledged). Takes the caller's open connection and does
        not commit — mirrors ``insert_ignore``'s batch-friendly shape so the
        caller (``TraderAgent``) controls the transaction boundary.
        """
        conn.execute(
            "UPDATE trades SET realised_pnl_ack_at = ? WHERE id = ?",
            (acknowledged_at, trade_id),
        )

    def delete_by_id(self, trade_id: int) -> bool:
        """Delete a trade by id. Returns True if a row was deleted."""
        with session(self._connect) as conn:
            cur = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
            return cur.rowcount > 0

    def delete_by_ticker(self, ticker: str, portfolio_id: int | None = None) -> None:
        """Delete every trade for a ticker, optionally within one portfolio."""
        with session(self._connect) as conn:
            if portfolio_id is None:
                conn.execute("DELETE FROM trades WHERE ticker = ?", (ticker,))
            else:
                conn.execute(
                    "DELETE FROM trades WHERE ticker = ? AND portfolio_id = ?",
                    (ticker, portfolio_id),
                )

    def history(
        self, ticker: str | None = None, portfolio_id: int | None = None
    ) -> list[Trade]:
        """Return trades newest-first, optionally filtered by ticker/portfolio."""
        base = (
            "SELECT id, ticker, action, shares, price, date, notes, stop_loss,"
            " entry_price, portfolio_id, realised_pnl_ack_at FROM trades"
        )
        order = f"{_DATE_SORT} DESC, id DESC"
        clauses: list[str] = []
        params: list[Any] = []
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker)
        if portfolio_id is not None:
            clauses.append("portfolio_id = ?")
            params.append(portfolio_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"{base}{where} ORDER BY {order}"
        with session(self._connect) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_trade(r) for r in rows]

    def open_rows(self, portfolio_id: int | None = None) -> list[tuple[Any, ...]]:
        """Return valid-ticker trade rows in chronological order for replay.

        Columns: (ticker, action, shares, price, date, stop_loss, entry_price).
        Excludes blank/``n/a`` tickers, matching the legacy portfolio query.
        Scoped to ``portfolio_id`` when given.
        """
        sql = (
            f"SELECT {_REPLAY_COLUMNS} FROM trades"
            " WHERE ticker NOT IN ('', 'n/a', 'N/A')"
        )
        params: tuple[Any, ...] = ()
        if portfolio_id is not None:
            sql += " AND portfolio_id = ?"
            params = (portfolio_id,)
        sql += f" ORDER BY {_DATE_SORT}, id"
        with session(self._connect) as conn:
            return conn.execute(sql, params).fetchall()

    def held_tickers(self) -> set[str]:
        """Return the set of tickers with a net-positive position in any
        portfolio (used for the watchlist "held" flag, which spans accounts)."""
        from collections import defaultdict

        by_pf: dict[Any, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT portfolio_id, ticker, action, shares FROM trades"
                " WHERE ticker NOT IN ('', 'n/a', 'N/A')"
                " ORDER BY date, id"
            ).fetchall()
        for pid, ticker, action, shares in rows:
            delta = shares if action == "BUY" else -shares
            by_pf[pid][ticker] += delta
        held: set[str] = set()
        for positions in by_pf.values():
            for ticker, net in positions.items():
                if net > 0:
                    held.add(ticker)
        return held
