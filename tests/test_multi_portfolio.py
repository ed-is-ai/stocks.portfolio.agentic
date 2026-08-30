"""Tests for multiple selectable portfolios (#147).

Covers the migration backfill into a default "SIPP" portfolio, per-portfolio
isolation of holdings/cash, delete scoping, per-portfolio import idempotency,
and the portfolio CRUD surface on ``TraderAgent``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
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


def test_get_cash_flows_surfaces_ledger_scoped_to_portfolio(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("SIPP", opening_cash=1000.0)
    b = agent.create_portfolio("ISA")
    a_flows = agent.get_cash_flows(a.id)
    assert [f.flow_type for f in a_flows] == ["OPENING"]
    assert a_flows[0].amount == 1000.0
    # The ISA has no opening cash, so its ledger is empty (isolation).
    assert agent.get_cash_flows(b.id) == []


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
    agent.import_sipp(csv_path.read_bytes(), pf.id, account_type_id="sipp")
    agent.import_sipp(
        csv_path.read_bytes(), pf.id, account_type_id="sipp"
    )  # re-import must not duplicate
    positions = agent.get_portfolio(portfolio_id=pf.id)
    assert len(positions) == 1
    assert positions[0].shares == 10


def test_same_csv_imports_into_two_portfolios_independently(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("SIPP")
    b = agent.create_portfolio("ISA")
    csv_path = _write_csv(tmp_path)
    agent.import_sipp(csv_path.read_bytes(), a.id, account_type_id="sipp")
    agent.import_sipp(
        csv_path.read_bytes(), b.id, account_type_id="sipp"
    )  # same references, different portfolio
    assert len(agent.get_portfolio(portfolio_id=a.id)) == 1
    assert len(agent.get_portfolio(portfolio_id=b.id)) == 1
    assert agent.get_cash_balance(a.id) == 5000.0
    assert agent.get_cash_balance(b.id) == 5000.0


def test_two_different_files_into_two_portfolios_never_cross_contaminate(
    tmp_path: Path,
) -> None:
    """AC #4: the Running Balance captured for one upload can never be
    attributed to the other. The cash-balance/ticker assertions below are
    the real proof, since each import parses its own caller-owned bytes
    (#210). The ``SIPP`` directory check is a regression guard specific to
    this call layer (``TraderAgent`` directly, bypassing the route) — the
    route-level proof that no such directory is ever created from an actual
    upload lives in ``tests/test_portfolio_import.py``."""
    agent = _agent(tmp_path)
    sipp = agent.create_portfolio("SIPP")
    isa = agent.create_portfolio("ISA")
    sipp_csv = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,AAPL,B0,10,100,Buy AAPL,SIPP-R1,1000,,4000\n"
    ).encode("utf-8")
    isa_csv = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,MSFT,B1,5,200,Buy MSFT,ISA-R1,1000,,9999\n"
    ).encode("utf-8")

    agent.import_sipp(sipp_csv, sipp.id, account_type_id="sipp")
    agent.import_sipp(isa_csv, isa.id, account_type_id="sipp")

    assert agent.get_cash_balance(sipp.id) == 4000.0
    assert agent.get_cash_balance(isa.id) == 9999.0
    sipp_tickers = {p.ticker for p in agent.get_portfolio(portfolio_id=sipp.id)}
    isa_tickers = {p.ticker for p in agent.get_portfolio(portfolio_id=isa.id)}
    assert sipp_tickers == {"AAPL"}
    assert isa_tickers == {"MSFT"}
    # No shared filesystem path was ever created for either import to race on.
    assert not (tmp_path / "SIPP").exists()


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
    agent.import_sipp(
        _write_rows(tmp_path, "fwd.csv", _MULTI_ROWS).read_bytes(),
        fwd.id,
        account_type_id="sipp",
    )
    agent.import_sipp(
        _write_rows(tmp_path, "rev.csv", list(reversed(_MULTI_ROWS))).read_bytes(),
        rev.id,
        account_type_id="sipp",
    )
    # Authoritative balance is the latest-dated row (2024-03-10 -> 2450),
    # whether the file is oldest-first or fully reversed.
    assert agent.get_cash_balance(fwd.id) == 2450.0
    assert agent.get_cash_balance(rev.id) == 2450.0
    # Positions are identical too (replay already sorts by date).
    fwd_pos = {p.ticker: p.shares for p in agent.get_portfolio(portfolio_id=fwd.id)}
    rev_pos = {p.ticker: p.shares for p in agent.get_portfolio(portfolio_id=rev.id)}
    assert fwd_pos == rev_pos == {"AAPL": 15.0, "MSFT": 5.0}


# Two separate files: an older quarter and a newer one, each a single row
# with its own closing Running Balance.
_OLD_FILE = ["01/01/2024,AAPL,B0,10,100,Buy AAPL,O1,1000,,4000"]
_NEW_FILE = ["01/06/2024,MSFT,B1,5,200,Buy MSFT,N1,1000,,9000"]


def test_cash_balance_does_not_regress_when_older_file_imported_after_newer(
    tmp_path: Path,
) -> None:
    # #160: importing an older SIPP file after a newer one must not overwrite
    # the balance with the older file's (stale) Running Balance.
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.import_sipp(
        _write_rows(tmp_path, "new.csv", _NEW_FILE).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 9000.0
    # Now import the OLDER file — balance must stay on the newer date.
    result = agent.import_sipp(
        _write_rows(tmp_path, "old.csv", _OLD_FILE).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 9000.0
    assert result.cash_balance == 9000.0
    with sqlite3.connect(agent.db_path) as conn:
        assert (
            conn.execute(
                "SELECT cash_balance FROM portfolio_snapshots "
                "WHERE portfolio_id = ? ORDER BY id DESC LIMIT 1",
                (pf.id,),
            ).fetchone()[0]
            == 9000.0
        )


def test_cash_balance_advances_when_newer_file_imported_after_older(
    tmp_path: Path,
) -> None:
    # The normal order still advances the balance to the newer date.
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.import_sipp(
        _write_rows(tmp_path, "old.csv", _OLD_FILE).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 4000.0
    result = agent.import_sipp(
        _write_rows(tmp_path, "new.csv", _NEW_FILE).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 9000.0
    # The effective-balance re-read must be right in the accepting direction
    # too, not just when the guard rejects -- a re-read that always returned
    # the prior balance would pass the rejection test and fail here.
    assert result.cash_balance == 9000.0
    with sqlite3.connect(agent.db_path) as conn:
        assert (
            conn.execute(
                "SELECT cash_balance FROM portfolio_snapshots "
                "WHERE portfolio_id = ? ORDER BY id DESC LIMIT 1",
                (pf.id,),
            ).fetchone()[0]
            == 9000.0
        )


# --- correct cash balance handling (Story 1.5) ------------------------------


def test_cash_balance_zero_running_balance_is_not_discarded(tmp_path: Path) -> None:
    """Story 1.5, AC1: a closing Running Balance of exactly 0.00 becomes the
    stored/displayed balance, not a stale prior positive value -- the
    ``rb > 0`` gate's defect discarded a genuine zero closing balance."""
    from decimal import Decimal

    from app.repositories.cash_balances_repo import CashBalancesRepository

    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.import_sipp(
        _write_rows(
            tmp_path,
            "first.csv",
            ["01/01/2024,n/a,n/a,n/a,n/a,Contribution,R1,,500.00,500.00"],
        ).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 500.0
    agent.import_sipp(
        _write_rows(
            tmp_path,
            "second.csv",
            ["01/02/2024,n/a,n/a,n/a,n/a,Withdrawal,R2,500.00,,0.00"],
        ).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 0.0
    cash_balances = CashBalancesRepository(db.make_connect(lambda: agent.db_path))
    assert cash_balances.get(pf.id, "GBP") == (Decimal("0.00"), "2024-02-01")


def test_cash_balance_negative_running_balance_is_accepted(tmp_path: Path) -> None:
    """Story 1.5, AC2: a negative closing Running Balance is accepted and
    stored as negative, not rejected or coerced to zero."""
    from decimal import Decimal

    from app.repositories.cash_balances_repo import CashBalancesRepository

    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.import_sipp(
        _write_rows(
            tmp_path,
            "sipp.csv",
            ["01/01/2024,n/a,n/a,n/a,n/a,Overdraft,R1,150.50,,-150.50"],
        ).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == -150.50
    cash_balances = CashBalancesRepository(db.make_connect(lambda: agent.db_path))
    assert cash_balances.get(pf.id, "GBP") == (Decimal("-150.50"), "2024-01-01")


def test_cash_balance_undated_running_balance_does_not_overwrite_dated(
    tmp_path: Path,
) -> None:
    """Story 1.5, AC3: an incoming balance with no parseable as-of date must
    never overwrite a stored balance that has a newer date."""
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.import_sipp(
        _write_rows(
            tmp_path,
            "dated.csv",
            ["01/01/2024,n/a,n/a,n/a,n/a,Contribution,R1,,500.00,500.00"],
        ).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 500.0
    agent.import_sipp(
        _write_rows(
            tmp_path,
            "undated.csv",
            ["not-a-date,n/a,n/a,n/a,n/a,Note,R2,,,999.00"],
        ).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 500.0


def test_cash_balance_undated_running_balance_applies_when_nothing_stored(
    tmp_path: Path,
) -> None:
    """Story 1.5, AC3 (negative case): the pre-existing fallback for a
    first-ever undated import must not regress -- when nothing is stored
    yet, an undated balance still applies."""
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.import_sipp(
        _write_rows(
            tmp_path,
            "undated.csv",
            ["not-a-date,n/a,n/a,n/a,n/a,Note,R1,,,750.00"],
        ).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 750.0


def test_cash_balance_same_day_tie_break_prefers_first_listed_row(
    tmp_path: Path,
) -> None:
    """Story 1.5, AC6: multiple rows sharing the same Date -- the closing
    balance is always the Running Balance from the first-listed row for
    that date, per the documented reverse-chronological export assumption
    (the first-listed same-day row is the most recent transaction of that
    day, PRD §10). Today's rank tuple picks the *last*-listed row instead
    -- the opposite direction."""
    same_day_rows = [
        "15/03/2024,n/a,n/a,n/a,n/a,Note A,RA,,,1000.00",
        "15/03/2024,n/a,n/a,n/a,n/a,Note B,RB,,,900.00",
        "15/03/2024,n/a,n/a,n/a,n/a,Note C,RC,,,800.00",
    ]
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.import_sipp(
        _write_rows(tmp_path, "sameday.csv", same_day_rows).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    assert agent.get_cash_balance(pf.id) == 1000.0


def test_cash_balance_same_day_tie_break_does_not_disturb_cross_date_ordering(
    tmp_path: Path,
) -> None:
    """The same-day tie-break fix must not change which *date* wins -- a
    later date's balance still always beats an earlier date's, regardless
    of how many rows tie within either date."""
    rows = [
        "01/01/2024,n/a,n/a,n/a,n/a,Opening,R0,,,100.00",
        "15/03/2024,n/a,n/a,n/a,n/a,Note A,RA,,,1000.00",
        "15/03/2024,n/a,n/a,n/a,n/a,Note B,RB,,,900.00",
    ]
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.import_sipp(
        _write_rows(tmp_path, "mixed.csv", rows).read_bytes(),
        pf.id,
        account_type_id="sipp",
    )
    # The later date (15/03) always wins over the earlier one (01/01), and
    # within it, the first-listed row (RA, 1000) wins the same-day tie.
    assert agent.get_cash_balance(pf.id) == 1000.0


# --- snapshots -------------------------------------------------------------


def test_snapshots_are_per_portfolio(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("A")
    b = agent.create_portfolio("B")
    agent.record_buy("AAPL", 10, 100, "2024-01-01", portfolio_id=a.id)
    agent.update_portfolio_snapshot(500.0, a.id)
    assert len(agent.snapshot_history(a.id)) == 1
    assert agent.snapshot_history(b.id) == []


def test_snapshot_history_since_filters_window_and_default_unchanged(
    tmp_path: Path,
) -> None:
    # #421: ``since`` adds ``AND timestamp >= ?``; omitting it is unchanged.
    agent = _agent(tmp_path)
    p = agent.create_portfolio("A")
    for ts, val in [
        ("2023-01-01T00:00:00+00:00", 100.0),
        ("2024-06-01T00:00:00+00:00", 200.0),
        ("2025-01-01T00:00:00+00:00", 300.0),
    ]:
        agent._snapshots.append(p.id, ts, val, val, None)

    everything = agent.snapshot_history(p.id)
    assert [r[1] for r in everything] == [100.0, 200.0, 300.0]  # oldest-first

    windowed = agent.snapshot_history(p.id, since="2024-01-01T00:00:00+00:00")
    assert [r[1] for r in windowed] == [200.0, 300.0]

    # Explicit None is byte-identical to omitting the argument.
    assert agent.snapshot_history(p.id, since=None) == everything


def test_snapshot_history_since_lifts_the_180_count_cap(tmp_path: Path) -> None:
    # #421: a ``since`` window is a time window, not a count window — the
    # default LIMIT 180 must not silently drop the oldest in-range rows.
    agent = _agent(tmp_path)
    p = agent.create_portfolio("A")
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(400):
        agent._snapshots.append(
            p.id, (base + timedelta(hours=i)).isoformat(), float(i), float(i), None
        )

    capped = agent.snapshot_history(p.id)
    assert len(capped) == 180  # legacy behaviour unchanged

    windowed = agent.snapshot_history(p.id, since="2024-01-01T00:00:00+00:00")
    assert len(windowed) == 400  # every in-range row, not just the newest 180
    assert [r[1] for r in windowed] == [float(i) for i in range(400)]
