"""
Trader Agent — records buy/sell trades to SQLite and computes portfolio P&L.

Standalone agent; not part of the main scan pipeline.
Run as part of the web UI backend.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ms_agent_framework import Agent
from models import Position, Trade

_DB_PATH = Path(__file__).parent / "trades.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK(action IN ('BUY', 'SELL')),
    shares      REAL NOT NULL CHECK(shares > 0),
    price       REAL NOT NULL CHECK(price > 0),
    date        TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    stop_loss   REAL,
    entry_price REAL
);
"""


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


class TraderAgent(Agent):
    """Records trades and computes portfolio P&L using average cost basis."""

    name: str = "TraderAgent"
    db_path: Path = _DB_PATH

    def model_post_init(self, __context: Any) -> None:
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(_SCHEMA)
            for col_def in ("stop_loss REAL", "entry_price REAL"):
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col_def}")
                except Exception:
                    pass
            conn.commit()

    def record_buy(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
        stop_loss: float | None = None,
        entry_price: float | None = None,
    ) -> Trade:
        """Record a buy transaction and return the saved Trade."""
        trade_date = date or datetime.today().strftime("%Y-%m-%d")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO trades (ticker, action, shares, price, date, notes,"
                " stop_loss, entry_price) VALUES (?, 'BUY', ?, ?, ?, ?, ?, ?)",
                (ticker.upper(), shares, price, trade_date, notes, stop_loss, entry_price),
            )
            trade_id: int = cur.lastrowid  # type: ignore[assignment]
        return Trade(
            id=trade_id,
            ticker=ticker.upper(),
            action="BUY",
            shares=shares,
            price=price,
            date=trade_date,
            notes=notes,
            stop_loss=stop_loss,
            entry_price=entry_price,
        )

    def record_sell(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
    ) -> Trade:
        """Record a sell transaction and return the saved Trade."""
        trade_date = date or datetime.today().strftime("%Y-%m-%d")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO trades (ticker, action, shares, price, date, notes)"
                " VALUES (?, 'SELL', ?, ?, ?, ?)",
                (ticker.upper(), shares, price, trade_date, notes),
            )
            trade_id: int = cur.lastrowid  # type: ignore[assignment]
        return Trade(
            id=trade_id,
            ticker=ticker.upper(),
            action="SELL",
            shares=shares,
            price=price,
            date=trade_date,
            notes=notes,
        )

    def correct_trade(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
        stop_loss: float | None = None,
        entry_price: float | None = None,
    ) -> Trade:
        """Overwrite the position for a ticker: delete all trades and insert one BUY."""
        trade_date = date or datetime.today().strftime("%Y-%m-%d")
        with self._conn() as conn:
            conn.execute("DELETE FROM trades WHERE ticker = ?", (ticker.upper(),))
            cur = conn.execute(
                "INSERT INTO trades (ticker, action, shares, price, date, notes,"
                " stop_loss, entry_price) VALUES (?, 'BUY', ?, ?, ?, ?, ?, ?)",
                (ticker.upper(), shares, price, trade_date, notes, stop_loss, entry_price),
            )
            trade_id: int = cur.lastrowid  # type: ignore[assignment]
        return Trade(
            id=trade_id,
            ticker=ticker.upper(),
            action="BUY",
            shares=shares,
            price=price,
            date=trade_date,
            notes=notes,
            stop_loss=stop_loss,
            entry_price=entry_price,
        )

    def delete_trade(self, trade_id: int) -> bool:
        """Delete a trade by ID. Returns True if a row was deleted."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
            return cur.rowcount > 0

    def get_trade_history(self, ticker: str | None = None) -> list[Trade]:
        """Return all trades, newest first. Optionally filter by ticker."""
        base = (
            "SELECT id, ticker, action, shares, price, date, notes, stop_loss, entry_price"
            " FROM trades"
        )
        if ticker:
            sql = base + " WHERE ticker = ? ORDER BY date DESC, id DESC"
            params: tuple[Any, ...] = (ticker.upper(),)
        else:
            sql = base + " ORDER BY date DESC, id DESC"
            params = ()
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_trade(r) for r in rows]

    def get_portfolio(
        self, current_prices: dict[str, float] | None = None
    ) -> list[Position]:
        """Compute open positions using average cost basis.

        Replays all trades chronologically to derive shares and avg cost per ticker.
        Unrealised P&L requires current_prices dict; omit for cost-only view.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ticker, action, shares, price, date, stop_loss, entry_price"
                " FROM trades ORDER BY date, id"
            ).fetchall()

        state = self._replay_trades(rows)
        return [
            self._build_position(ticker, s, current_prices)
            for ticker, s in state.items()
            if s["shares"] > 0
        ]

    @staticmethod
    def _replay_trades(
        rows: list[tuple[Any, ...]],
    ) -> dict[str, dict[str, Any]]:
        """Replay trade rows to derive per-ticker running state."""
        state: dict[str, dict[str, Any]] = {}
        for ticker, action, shares, price, date, stop_loss, entry_price in rows:
            if ticker not in state:
                state[ticker] = {
                    "shares": 0.0,
                    "avg_cost": 0.0,
                    "entry_date": None,
                    "entry_price": None,
                    "stop_loss": None,
                }
            s = state[ticker]
            if action == "BUY":
                new_total = s["shares"] + shares
                s["avg_cost"] = (s["avg_cost"] * s["shares"] + price * shares) / new_total
                s["shares"] = new_total
                if s["entry_date"] is None:
                    s["entry_date"] = date
                if stop_loss is not None:
                    s["stop_loss"] = stop_loss
                if entry_price is not None:
                    s["entry_price"] = entry_price
            else:  # SELL
                s["shares"] = max(0.0, s["shares"] - shares)
        return state

    @staticmethod
    def _build_position(
        ticker: str,
        s: dict[str, Any],
        current_prices: dict[str, float] | None,
    ) -> Position:
        """Build a Position model from replay state and live prices."""
        avg_cost = s["avg_cost"]
        remaining = s["shares"]
        total_cost = round(avg_cost * remaining, 2)
        cp = current_prices.get(ticker) if current_prices else None
        current_value = round(cp * remaining, 2) if cp is not None else None
        upnl = round(current_value - total_cost, 2) if current_value is not None else None
        upnl_pct = (
            round(upnl / total_cost * 100, 2)
            if upnl is not None and total_cost > 0
            else None
        )
        entry_price: float | None = s["entry_price"]
        pt20 = round(entry_price * 1.20, 2) if entry_price else None
        pt25 = round(entry_price * 1.25, 2) if entry_price else None
        return Position(
            ticker=ticker,
            shares=round(remaining, 4),
            avg_cost=round(avg_cost, 4),
            current_price=round(cp, 2) if cp is not None else None,
            total_cost=total_cost,
            current_value=current_value,
            unrealised_pnl=upnl,
            unrealised_pnl_pct=upnl_pct,
            entry_price=entry_price,
            entry_date=s["entry_date"],
            stop_loss=s["stop_loss"],
            profit_target_20=pt20,
            profit_target_25=pt25,
        )

    def run(self, payload: Any = None) -> list[Position]:
        """Standalone agent interface — return current portfolio."""
        return self.get_portfolio()
