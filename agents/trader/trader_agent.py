"""
Trader Agent — records buy/sell trades to SQLite and computes portfolio P&L.

Standalone agent; not part of the main scan pipeline.
Run as part of the web UI backend.
"""

import csv
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
CREATE TABLE IF NOT EXISTS cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    flow_type   TEXT NOT NULL CHECK(flow_type IN ('CONTRIBUTION', 'DIVIDEND', 'INTEREST', 'TAX_RELIEF', 'TRANSFER', 'WITHDRAWAL', 'OTHER')),
    ticker      TEXT,
    amount      REAL NOT NULL CHECK(amount > 0),
    description TEXT,
    reference   TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS price_cache (
    ticker          TEXT PRIMARY KEY,
    price           REAL NOT NULL,
    fetched_at      TEXT NOT NULL,
    currency        TEXT DEFAULT 'GBP',
    original_price  REAL
);
CREATE TABLE IF NOT EXISTS account_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    """Records trades and computes portfolio P&L using average cost basis.

    Supports importing SIPP transactions from CSV, separating stock trades
    from non-trade cash flows (contributions, dividends, tax relief, interest).
    Maintains separate tables: trades (stock buy/sell) and cash_flows (non-trades).
    """

    name: str = "TraderAgent"
    db_path: Path = _DB_PATH

    def model_post_init(self, __context: Any) -> None:
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            for col_def in ("stop_loss REAL", "entry_price REAL"):
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col_def}")
                except Exception:
                    pass
            for col_def in ("currency TEXT DEFAULT 'GBP'", "original_price REAL"):
                try:
                    conn.execute(f"ALTER TABLE price_cache ADD COLUMN {col_def}")
                except Exception:
                    pass
            # Seed cash_balance from portfolio_value.csv if not yet stored
            has_cash = conn.execute(
                "SELECT 1 FROM account_state WHERE key='cash_balance'"
            ).fetchone()
            if not has_cash:
                self._seed_cash_from_csv(conn)
            conn.commit()

    def _seed_cash_from_csv(self, conn: sqlite3.Connection) -> None:
        """Seed cash_balance from portfolio_value.csv on first startup."""
        pv_csv = self.db_path.parent.parent.parent / "portfolio_value.csv"
        if not pv_csv.exists():
            return
        try:
            with open(pv_csv, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            if rows and "cash_balance" in rows[-1]:
                amount = float(rows[-1]["cash_balance"])
                if amount > 0:
                    from datetime import datetime, timezone
                    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    conn.execute(
                        "INSERT OR IGNORE INTO account_state (key, value, updated_at)"
                        " VALUES ('cash_balance', ?, ?)",
                        (str(amount), updated_at),
                    )
        except Exception:
            pass

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
                (
                    ticker.upper(),
                    shares,
                    price,
                    trade_date,
                    notes,
                    stop_loss,
                    entry_price,
                ),
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
                (
                    ticker.upper(),
                    shares,
                    price,
                    trade_date,
                    notes,
                    stop_loss,
                    entry_price,
                ),
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
        date_order = "substr(date, 7, 4) || '/' || substr(date, 4, 2) || '/' || substr(date, 1, 2) DESC, id DESC"
        if ticker:
            sql = base + " WHERE ticker = ? ORDER BY " + date_order
            params: tuple[Any, ...] = (ticker.upper(),)
        else:
            sql = base + " ORDER BY " + date_order
            params = ()
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_trade(r) for r in rows]

    def get_portfolio(
        self,
        current_prices: dict[str, float] | None = None,
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> list[Position]:
        """Compute open positions using average cost basis.

        Replays all trades chronologically to derive shares and avg cost per ticker.
        Unrealised P&L requires current_prices dict; omit for cost-only view.
        Only includes valid-ticker trades (excludes Symbol='n/a').
        display_info: optional {ticker: (original_price, currency)} for template display.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ticker, action, shares, price, date, stop_loss, entry_price"
                " FROM trades WHERE ticker NOT IN ('', 'n/a', 'N/A')"
                " ORDER BY substr(date, 7, 4) || '/' || substr(date, 4, 2) || '/' || substr(date, 1, 2), id"
            ).fetchall()

        state = self._replay_trades(rows)
        return [
            self._build_position(ticker, s, current_prices, display_info)
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
                s["avg_cost"] = (
                    s["avg_cost"] * s["shares"] + price * shares
                ) / new_total
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
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> Position:
        """Build a Position model from replay state and live prices.

        For USD stocks, all monetary fields (current_price, current_value,
        unrealised_pnl) are kept in USD so that P&L is self-consistent.
        The web layer converts to GBP for aggregate totals.
        """
        avg_cost = s["avg_cost"]
        remaining = s["shares"]
        total_cost = round(avg_cost * remaining, 2)

        disp = display_info.get(ticker) if display_info else None
        currency = disp[1] if disp else "GBP"
        is_usd = currency == "USD"

        if is_usd and disp is not None:
            # Use original USD price so cost (USD) and value (USD) are comparable
            cp: float | None = disp[0]
        else:
            cp = current_prices.get(ticker) if current_prices else None

        current_value = round(cp * remaining, 2) if cp is not None else None
        upnl = (
            round(current_value - total_cost, 2) if current_value is not None else None
        )
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
            price_currency=currency,
        )

    def update_portfolio_snapshot(self, cash_balance: float | None = None) -> None:
        """Update portfolio_value.csv with total_value, total_cost, cash_balance, investments_value."""
        from datetime import datetime, timezone
        import json

        portfolio_csv = Path(__file__).parent.parent.parent / "portfolio_value.csv"

        # Load current prices from analysis results
        analysis_json = (
            Path(__file__).parent.parent.parent
            / "agents"
            / "analyst"
            / "analysis_results.json"
        )
        prices = {}
        if analysis_json.exists():
            try:
                data = json.loads(analysis_json.read_text(encoding="utf-8"))
                prices = {r["ticker"]: r["price"] for r in data}
            except Exception:
                pass

        positions = self.get_portfolio(prices if prices else None)
        total_cost = sum(p.total_cost for p in positions if p.total_cost is not None)
        total_value = sum(
            p.current_value for p in positions if p.current_value is not None
        )
        if total_value is None:
            total_value = total_cost

        if cash_balance is None:
            if portfolio_csv.exists():
                try:
                    with open(portfolio_csv) as f:
                        reader = csv.DictReader(f)
                        rows = list(reader)
                        if rows:
                            cash_balance = float(rows[-1].get("cash_balance", "0"))
                        else:
                            cash_balance = 0.0
                except Exception:
                    cash_balance = 0.0
            else:
                cash_balance = 0.0

        timestamp = datetime.now(timezone.utc).isoformat()
        investments_value = total_cost

        with open(portfolio_csv, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp",
                    "total_value",
                    "total_cost",
                    "cash_balance",
                    "investments_value",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "timestamp": timestamp,
                    "total_value": f"{total_value:.2f}",
                    "total_cost": f"{total_cost:.2f}",
                    "cash_balance": f"{cash_balance:.2f}",
                    "investments_value": f"{investments_value:.2f}",
                }
            )

    def import_sipp(self, csv_path: str | Path) -> float:
        """Import SIPP CSV, extract trades and cash flows. Return cash balance."""
        csv_path = Path(csv_path)

        def clean_amount(s: str | None) -> float:
            if not s or s.strip() in ("", "n/a"):
                return 0.0
            s = (
                s.replace("﻿", "")
                .replace("£", "")
                .replace(",", "")
                .replace('"', "")
                .strip()
            )
            try:
                return float(s)
            except ValueError:
                return 0.0

        def classify_flow_type(description: str) -> str:
            d = description.lower()
            if "contribution" in d:
                return "CONTRIBUTION"
            elif "div" in d:
                return "DIVIDEND"
            elif "interest" in d:
                return "INTEREST"
            elif "tax relief" in d:
                return "TAX_RELIEF"
            elif "trf" in d:
                return "TRANSFER"
            elif "debit" in d:
                return "WITHDRAWAL"
            return "OTHER"

        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        conn = self._conn()
        buy_count, sell_count, cash_count, cash_balance = 0, 0, 0, 0.0

        for row in rows:
            qty = row.get("Quantity", "").strip()
            symbol = row.get("Symbol", "").strip()
            sedol = row.get("Sedol", "").strip()
            debit = clean_amount(row.get("Debit", ""))
            credit = clean_amount(row.get("Credit", ""))
            price = clean_amount(row.get("Price", ""))
            date = row.get("Date", "").strip()
            description = row.get("Description", "").strip()
            reference = row.get("Reference", "").strip()

            if qty and qty != "n/a":
                # Trade if Symbol is valid or if HSBC GLOB
                is_trade = symbol and symbol != "n/a"
                is_hsbc_glob = "HSBC GLOB" in description.upper()

                if is_trade or is_hsbc_glob:
                    try:
                        shares = float(qty.replace("£", "").replace(",", ""))
                        if shares > 0 and price > 0:
                            action = (
                                "BUY" if debit > 0 else "SELL" if credit > 0 else None
                            )
                            if action:
                                ticker = symbol.upper() if is_trade else "HSFWA"
                                conn.execute(
                                    "INSERT INTO trades (ticker, action, shares, price, date, notes) "
                                    "VALUES (?, ?, ?, ?, ?, ?)",
                                    (ticker, action, shares, price, date, ""),
                                )
                                if action == "BUY":
                                    buy_count += 1
                                else:
                                    sell_count += 1
                    except ValueError:
                        pass
                else:
                    amount = credit if credit > 0 else debit
                    if amount > 0:
                        flow_type = classify_flow_type(description)
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO cash_flows "
                                "(date, flow_type, ticker, amount, description, reference) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (date, flow_type, None, amount, description, reference),
                            )
                            cash_count += 1
                        except Exception:
                            pass
            else:
                if symbol and symbol != "n/a":
                    continue
                amount = credit if credit > 0 else debit
                if amount > 0:
                    flow_type = classify_flow_type(description)
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO cash_flows "
                            "(date, flow_type, ticker, amount, description, reference) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (date, flow_type, None, amount, description, reference),
                        )
                        cash_count += 1
                    except Exception:
                        pass

            running_balance = row.get("Running Balance", "").strip()
            if running_balance and running_balance != "n/a":
                rb = clean_amount(running_balance)
                if rb > 0:
                    cash_balance = rb

        conn.commit()
        conn.close()

        if cash_balance > 0:
            self.set_cash_balance(cash_balance)
        self.update_portfolio_snapshot(cash_balance)

        print(
            f"SIPP import complete: {buy_count} BUY, {sell_count} SELL, "
            f"{cash_count} cash flows. Cash balance: GBP {cash_balance:,.2f}"
        )
        return cash_balance

    def save_price_cache(
        self,
        prices: dict[str, float],
        currencies: dict[str, tuple[float, str]] | None = None,
    ) -> None:
        """Persist fetched prices to the DB with a UTC timestamp.

        currencies: optional {ticker: (original_price, currency_code)} for display.
        """
        from datetime import datetime, timezone

        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with self._conn() as conn:
            for t, p in prices.items():
                orig = currencies.get(t) if currencies else None
                orig_price = orig[0] if orig else None
                currency = orig[1] if orig else "GBP"
                conn.execute(
                    "INSERT INTO price_cache (ticker, price, fetched_at, currency, original_price)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(ticker) DO UPDATE SET"
                    "  price=excluded.price, fetched_at=excluded.fetched_at,"
                    "  currency=excluded.currency, original_price=excluded.original_price",
                    (t, p, fetched_at, currency, orig_price),
                )
            conn.commit()

    def load_price_cache(
        self,
    ) -> tuple[dict[str, float], str | None, dict[str, tuple[float, str]]]:
        """Return (prices, fetched_at, display_info) from the DB.

        display_info: {ticker: (original_price, currency_code)}
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ticker, price, fetched_at, currency, original_price FROM price_cache"
            ).fetchall()
        if not rows:
            return {}, None, {}
        prices = {r[0]: r[1] for r in rows}
        fetched_at = rows[0][2]
        display_info: dict[str, tuple[float, str]] = {
            r[0]: (r[4], r[3] or "GBP")
            for r in rows
            if r[4] is not None
        }
        return prices, fetched_at, display_info

    def set_cash_balance(self, amount: float) -> None:
        """Persist the SIPP cash balance (Running Balance) to account_state."""
        from datetime import datetime, timezone

        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO account_state (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("cash_balance", str(amount), updated_at),
            )
            conn.commit()

    def get_cash_balance(self) -> float | None:
        """Return the stored SIPP cash balance, or None if not yet imported."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM account_state WHERE key='cash_balance'"
            ).fetchone()
        return float(row[0]) if row else None

    def refresh_portfolio_prices(
        self,
        current_prices: dict[str, float],
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> list[Position]:
        """Fetch live prices and recalculate portfolio metrics.

        Takes a dict of current prices {ticker: gbp_price} and returns updated
        Position objects with recalculated market value, unrealised P&L,
        and profit targets. Handles missing prices gracefully.
        display_info: optional {ticker: (original_price, currency)} for template display.
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.info("Starting portfolio price refresh")

        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ticker, action, shares, price, date, stop_loss, entry_price"
                " FROM trades WHERE ticker NOT IN ('', 'n/a', 'N/A')"
                " ORDER BY substr(date, 7, 4) || '/' || substr(date, 4, 2) || '/' || substr(date, 1, 2), id"
            ).fetchall()

        state = self._replay_trades(rows)
        positions = []

        missing_tickers = [
            ticker for ticker, s in state.items()
            if s["shares"] > 0 and ticker not in current_prices
        ]
        if missing_tickers:
            logger.warning(f"Could not fetch prices for: {missing_tickers}")

        for ticker, s in state.items():
            if s["shares"] > 0:
                pos = self._build_position(ticker, s, current_prices, display_info)
                positions.append(pos)

        logger.info(f"Portfolio refresh complete: {len(positions)} positions updated")
        return positions

    def run(self, payload: Any = None) -> list[Position]:
        """Standalone agent interface — return current portfolio."""
        return self.get_portfolio()
