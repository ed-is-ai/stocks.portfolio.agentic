"""Tests for the historical snapshot repair pass (#466)."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.repositories import db
from app.services.snapshot_repair import (
    NoHistoricalPriceSource,
    SnapshotRepairService,
)


class _FixedPriceSource:
    """A historical source with evidence for a fixed ``{ticker: price}`` set."""

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices
        self.calls: list[tuple[str, str]] = []

    def gbp_price(self, ticker: str, as_of: str) -> float | None:
        self.calls.append((ticker, as_of))
        return self._prices.get(ticker)


def _agent(tmp_path: Path) -> TraderAgent:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    return agent


def _service(agent: TraderAgent, source: object | None = None) -> SnapshotRepairService:
    return SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        source,  # type: ignore[arg-type]
    )


def _values(agent: TraderAgent, portfolio_id: int) -> list[object]:
    return [row[1] for row in agent.snapshot_history(portfolio_id)]


def test_zero_row_with_holdings_and_no_evidence_becomes_null(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)

    report = _service(agent, NoHistoricalPriceSource()).repair()

    assert (report.candidates, report.marked_unavailable, report.repaired) == (1, 1, 0)
    assert _values(agent, pf.id) == [None]


def test_zero_row_is_reconstructed_from_historical_evidence(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    source = _FixedPriceSource({"AAPL": 7.5})

    report = _service(agent, source).repair()

    assert (report.repaired, report.marked_unavailable) == (1, 0)
    assert _values(agent, pf.id) == [pytest.approx(75.0)]
    assert source.calls == [("AAPL", "2024-02-01")]


def test_reconstructed_zero_counts_as_unavailable(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)

    report = _service(agent, _FixedPriceSource({"AAPL": 0.0})).repair()

    assert (report.repaired, report.marked_unavailable) == (0, 1)
    assert _values(agent, pf.id) == [None]


def test_cash_only_zero_and_valid_rows_are_left_untouched(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    # A genuinely empty portfolio's 0.00 is correct; a priced row is valid.
    agent._snapshots.append(pf.id, "2024-01-01T00:00:00+00:00", 0.0, 0.0, 100.0)
    agent._snapshots.append(pf.id, "2024-03-01T00:00:00+00:00", 1234.0, 900.0, 100.0)

    report = _service(agent, NoHistoricalPriceSource()).repair()

    assert (report.scanned, report.candidates, report.unchanged) == (2, 0, 2)
    assert _values(agent, pf.id) == [0.0, pytest.approx(1234.0)]


def test_repair_is_idempotent(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    service = _service(agent, NoHistoricalPriceSource())

    service.repair()
    second = service.repair()

    assert (second.candidates, second.repaired, second.marked_unavailable) == (0, 0, 0)
    assert second.unchanged == 1
    assert _values(agent, pf.id) == [None]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)

    report = _service(agent, NoHistoricalPriceSource()).repair(dry_run=True)

    assert report.marked_unavailable == 1
    assert _values(agent, pf.id) == [0.0]


def test_trades_after_the_snapshot_do_not_make_it_a_candidate(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-06-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 0.0, 100.0)

    report = _service(agent, NoHistoricalPriceSource()).repair()

    assert report.candidates == 0
    assert _values(agent, pf.id) == [0.0]


def test_repair_scopes_to_one_portfolio(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("A")
    b = agent.create_portfolio("B")
    for pid in (a.id, b.id):
        agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pid)
        agent._snapshots.append(pid, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)

    _service(agent, NoHistoricalPriceSource()).repair(portfolio_id=a.id)

    assert _values(agent, a.id) == [None]
    assert _values(agent, b.id) == [0.0]


def test_legacy_not_null_schema_is_migrated_preserving_rows(tmp_path: Path) -> None:
    """A pre-#466 database can store NULL after ``init_trades_db`` runs."""
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE portfolio_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id   INTEGER NOT NULL,
            timestamp      TEXT NOT NULL,
            total_value    REAL NOT NULL,
            total_cost     REAL NOT NULL,
            cash_balance   REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO portfolio_snapshots "
        "(id, portfolio_id, timestamp, total_value, total_cost, cash_balance) "
        "VALUES (7, 1, '2024-01-01T00:00:00+00:00', 100.0, 90.0, 10.0)"
    )
    conn.commit()
    db.init_trades_db(conn)
    conn.commit()

    info = conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()
    notnull = {row[1]: row[3] for row in info}
    assert notnull["total_value"] == 0
    assert notnull["total_cost"] == 0
    assert conn.execute(
        "SELECT id, total_value, total_cost FROM portfolio_snapshots"
    ).fetchall() == [(7, 100.0, 90.0)]

    # Idempotent: a second run is a no-op and the row survives.
    db.init_trades_db(conn)
    conn.commit()
    conn.execute("UPDATE portfolio_snapshots SET total_value = NULL WHERE id = 7")
    assert conn.execute(
        "SELECT total_value FROM portfolio_snapshots WHERE id = 7"
    ).fetchone() == (None,)
    conn.close()


def test_repair_script_migrates_a_legacy_not_null_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standalone CLI must self-migrate rather than crash on a fresh DB.

    ``scripts/repair_portfolio_snapshots.py`` connects directly, bypassing
    ``TraderAgent.model_post_init`` -- the only other place that has ever
    called ``init_trades_db``. Before this test's fix, running the script
    against a database that predates #466 (columns still ``NOT NULL``) would
    raise ``sqlite3.IntegrityError`` the moment it tried to write ``NULL``.
    """
    db_path = tmp_path / "trades.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE portfolio_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id   INTEGER NOT NULL,
            timestamp      TEXT NOT NULL,
            total_value    REAL NOT NULL,
            total_cost     REAL NOT NULL,
            cash_balance   REAL
        );
        CREATE TABLE trades (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker         TEXT NOT NULL,
            action         TEXT NOT NULL,
            shares         REAL NOT NULL,
            price          REAL NOT NULL,
            date           TEXT NOT NULL,
            notes          TEXT,
            portfolio_id   INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO trades (ticker, action, shares, price, date, portfolio_id) "
        "VALUES ('AAPL', 'BUY', 10, 5.0, '2024-01-01', 1)"
    )
    conn.execute(
        "INSERT INTO portfolio_snapshots "
        "(portfolio_id, timestamp, total_value, total_cost, cash_balance) "
        "VALUES (1, '2024-02-01T00:00:00+00:00', 0.0, 50.0, 100.0)"
    )
    conn.commit()
    conn.close()

    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "repair_portfolio_snapshots.py"
    )
    spec = importlib.util.spec_from_file_location(
        "repair_portfolio_snapshots", script_path
    )
    assert spec is not None and spec.loader is not None
    repair_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair_script)

    monkeypatch.setattr(repair_script, "TRADES_DB", db_path)

    repair_script.main([])

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT total_value FROM portfolio_snapshots").fetchall() == [
        (None,)
    ]
    conn.close()
