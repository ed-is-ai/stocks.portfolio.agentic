"""Repository for the ``trades`` table in ``trades.db``."""

from typing import Any

from models import Trade

from app.repositories.db import Connect, session

# Replays sort DD/MM/YYYY dates by reconstructing YYYY/MM/DD, then id.
_DATE_SORT = (
    "substr(date, 7, 4) || '/' || substr(date, 4, 2) || '/' || substr(date, 1, 2)"
)
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
    ) -> int:
        """Insert a trade and return its new id."""
        with session(self._connect) as conn:
            cur = conn.execute(
                "INSERT INTO trades (ticker, action, shares, price, date, notes,"
                " stop_loss, entry_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, action, shares, price, date, notes, stop_loss, entry_price),
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
    ) -> None:
        """Insert a trade with ``INSERT OR IGNORE`` on the given connection.

        Used by the SIPP import, which batches many rows in one transaction.
        """
        conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(ticker, action, shares, price, date, notes, reference) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticker, action, shares, price, date, notes, reference),
        )

    def delete_by_id(self, trade_id: int) -> bool:
        """Delete a trade by id. Returns True if a row was deleted."""
        with session(self._connect) as conn:
            cur = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
            return cur.rowcount > 0

    def delete_by_ticker(self, ticker: str) -> None:
        """Delete every trade for a ticker."""
        with session(self._connect) as conn:
            conn.execute("DELETE FROM trades WHERE ticker = ?", (ticker,))

    def history(self, ticker: str | None = None) -> list[Trade]:
        """Return trades newest-first, optionally filtered by ticker."""
        base = (
            "SELECT id, ticker, action, shares, price, date, notes, stop_loss,"
            " entry_price FROM trades"
        )
        order = f"{_DATE_SORT} DESC, id DESC"
        if ticker:
            sql = f"{base} WHERE ticker = ? ORDER BY {order}"
            params: tuple[Any, ...] = (ticker,)
        else:
            sql = f"{base} ORDER BY {order}"
            params = ()
        with session(self._connect) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_trade(r) for r in rows]

    def open_rows(self) -> list[tuple[Any, ...]]:
        """Return valid-ticker trade rows in chronological order for replay.

        Columns: (ticker, action, shares, price, date, stop_loss, entry_price).
        Excludes blank/``n/a`` tickers, matching the legacy portfolio query.
        """
        with session(self._connect) as conn:
            return conn.execute(
                f"SELECT {_REPLAY_COLUMNS} FROM trades"
                " WHERE ticker NOT IN ('', 'n/a', 'N/A')"
                f" ORDER BY {_DATE_SORT}, id"
            ).fetchall()
