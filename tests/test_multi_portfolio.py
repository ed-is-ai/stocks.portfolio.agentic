"""Tests for multiple selectable portfolios (#147).

Covers the migration backfill into a default "SIPP" portfolio, per-portfolio
isolation of holdings/cash, delete scoping, per-portfolio import idempotency,
and the portfolio CRUD surface on ``TraderAgent``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.agents.trader.trader_agent import TraderAgent
from app.repositories import db

# Pre-multi-portfolio schema, used to build a legacy database for the migration
# test: no ``portfolio_id`` anywhere and a globally-unique cash-flow reference.
_LEGACY_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, action TEXT,
    shares REAL, price REAL, date TEXT, notes TEXT DEFAULT '',
    stop_loss REAL, entry_price REAL, reference TEXT
);
CREATE TABLE cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, flow_type TEXT,
    ticker TEXT, amount REAL, description TEXT, reference TEXT UNIQUE
);
CREATE TABLE account_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""


def _agent(tmp_path: Path) -> TraderAgent:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    return agent


# --- migration -------------------------------------------------------------


def test_migration_backfills_legacy_data_into_sipp(tmp_path: Path) -> None:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO trades (ticker, action, shares, price, date) "
        "VALUES ('AAPL', 'BUY', 10, 100, '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO cash_flows (date, flow_type, amount, reference) "
        "VALUES ('2024-01-01', 'DIVIDEND', 12.5, 'R1')"
    )
    conn.execute(
        "INSERT INTO account_state (key, value, updated_at) "
        "VALUES ('cash_balance', '5000.0', 'now')"
    )
    conn.commit()

    db.init_trades_db(conn)

    portfolios = conn.execute("SELECT id, name FROM portfolios").fetchall()
    assert len(portfolios) == 1
    pid, name = portfolios[0]
    assert name == db.DEFAULT_PORTFOLIO_NAME == "SIPP"
    assert conn.execute("SELECT portfolio_id FROM trades").fetchone()[0] == pid
    assert conn.execute("SELECT portfolio_id FROM cash_flows").fetchone()[0] == pid
    # Cash balance is re-keyed per-portfolio.
    keys = {r[0] for r in conn.execute("SELECT key FROM account_state")}
    assert f"cash_balance:{pid}" in keys
    assert "cash_balance" not in keys
    conn.close()


def test_fresh_database_starts_with_no_portfolios(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    assert agent.list_portfolios() == []


# --- CRUD ------------------------------------------------------------------


def test_create_portfolio_with_opening_cash_records_cashflow(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("ISA", opening_cash=2500.0)
    assert pf.name == "ISA"
    assert agent.get_cash_balance(pf.id) == 2500.0
    # Opening cash is auditable in trade/cash history.
    assert pf.cash_flow_count == 1


def test_rename_and_delete_portfolio(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("ISA")
    assert agent.rename_portfolio(pf.id, "Stocks & Shares ISA") is True
    renamed = agent.get_portfolio_meta(pf.id)
    assert renamed is not None and renamed.name == "Stocks & Shares ISA"
    assert agent.delete_portfolio(pf.id) is True
    assert agent.get_portfolio_meta(pf.id) is None


# --- isolation -------------------------------------------------------------


def test_holdings_are_isolated_per_portfolio(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    sipp = agent.create_portfolio("SIPP")
    isa = agent.create_portfolio("ISA")
    agent.record_buy("AAPL", 10, 100, "2024-01-01", portfolio_id=sipp.id)
    agent.record_buy("MSFT", 5, 200, "2024-01-01", portfolio_id=isa.id)

    sipp_tickers = {p.ticker for p in agent.get_portfolio(portfolio_id=sipp.id)}
    isa_tickers = {p.ticker for p in agent.get_portfolio(portfolio_id=isa.id)}
    assert sipp_tickers == {"AAPL"}
    assert isa_tickers == {"MSFT"}
    # Held-tickers spans every portfolio (watchlist "held" flag).
    assert agent.held_tickers() == {"AAPL", "MSFT"}


def test_cash_is_isolated_per_portfolio(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("A", opening_cash=100.0)
    b = agent.create_portfolio("B", opening_cash=250.0)
    assert agent.get_cash_balance(a.id) == 100.0
    assert agent.get_cash_balance(b.id) == 250.0


def test_delete_removes_only_that_portfolios_data(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    sipp = agent.create_portfolio("SIPP")
    isa = agent.create_portfolio("ISA")
    agent.record_buy("AAPL", 10, 100, "2024-01-01", portfolio_id=sipp.id)
    agent.record_buy("MSFT", 5, 200, "2024-01-01", portfolio_id=isa.id)

    agent.delete_portfolio(sipp.id)
    assert agent.get_portfolio_meta(sipp.id) is None
    # ISA data is untouched.
    assert {p.ticker for p in agent.get_portfolio(portfolio_id=isa.id)} == {"MSFT"}
    # No orphaned SIPP trades linger in the aggregate view.
    assert {p.ticker for p in agent.get_portfolio()} == {"MSFT"}


# --- import idempotency ----------------------------------------------------

_CSV = (
    "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
    "Running Balance\n"
    "01/01/2024,AAPL,B0,10,100,Buy AAPL,REF1,1000,,5000\n"
)


def _write_csv(tmp_path: Path) -> Path:
    csv_path = tmp_path / "merged.csv"
    csv_path.write_text(_CSV, encoding="utf-8")
    return csv_path


def test_import_is_idempotent_within_a_portfolio(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    csv_path = _write_csv(tmp_path)
    agent.import_sipp(csv_path, pf.id)
    agent.import_sipp(csv_path, pf.id)  # re-import must not duplicate
    positions = agent.get_portfolio(portfolio_id=pf.id)
    assert len(positions) == 1
    assert positions[0].shares == 10


def test_same_csv_imports_into_two_portfolios_independently(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("SIPP")
    b = agent.create_portfolio("ISA")
    csv_path = _write_csv(tmp_path)
    agent.import_sipp(csv_path, a.id)
    agent.import_sipp(csv_path, b.id)  # same references, different portfolio
    assert len(agent.get_portfolio(portfolio_id=a.id)) == 1
    assert len(agent.get_portfolio(portfolio_id=b.id)) == 1
    assert agent.get_cash_balance(a.id) == 5000.0
    assert agent.get_cash_balance(b.id) == 5000.0


# --- import row order (#158) -----------------------------------------------

_MULTI_HEADER = (
    "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
    "Running Balance"
)
# Three dated trade rows, oldest-first; the latest-dated row (2024-03-10)
# carries the authoritative closing balance of 2450.
_MULTI_ROWS = [
    "01/01/2024,AAPL,B0,10,100,Buy AAPL,R1,1000,,4000",
    "15/02/2024,MSFT,B1,5,200,Buy MSFT,R2,1000,,3000",
    "10/03/2024,AAPL,B0,5,110,Buy AAPL,R3,550,,2450",
]


def _write_rows(tmp_path: Path, name: str, rows: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join([_MULTI_HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def test_cash_balance_is_independent_of_row_order(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    fwd = agent.create_portfolio("Forward")
    rev = agent.create_portfolio("Reversed")
    agent.import_sipp(_write_rows(tmp_path, "fwd.csv", _MULTI_ROWS), fwd.id)
    agent.import_sipp(
        _write_rows(tmp_path, "rev.csv", list(reversed(_MULTI_ROWS))), rev.id
    )
    # Authoritative balance is the latest-dated row (2024-03-10 -> 2450),
    # whether the file is oldest-first or fully reversed.
    assert agent.get_cash_balance(fwd.id) == 2450.0
    assert agent.get_cash_balance(rev.id) == 2450.0
    # Positions are identical too (replay already sorts by date).
    fwd_pos = {p.ticker: p.shares for p in agent.get_portfolio(portfolio_id=fwd.id)}
    rev_pos = {p.ticker: p.shares for p in agent.get_portfolio(portfolio_id=rev.id)}
    assert fwd_pos == rev_pos == {"AAPL": 15.0, "MSFT": 5.0}


# --- snapshots -------------------------------------------------------------


def test_snapshots_are_per_portfolio(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("A")
    b = agent.create_portfolio("B")
    agent.record_buy("AAPL", 10, 100, "2024-01-01", portfolio_id=a.id)
    agent.update_portfolio_snapshot(500.0, a.id)
    assert len(agent.snapshot_history(a.id)) == 1
    assert agent.snapshot_history(b.id) == []
