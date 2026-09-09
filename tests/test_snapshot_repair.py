"""Tests for the historical snapshot repair pass (#466)."""

from __future__ import annotations

import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.cli import repair_portfolio_snapshots as repair_cli
from app.repositories import db
from app.services.backtest.historical_price_evidence import FX_PAIR
from app.services.snapshot_price_backfill import PriceEvidenceUnavailable
from app.services.snapshot_repair import (
    NoHistoricalPriceSource,
    SnapshotRepairService,
    cost_basis_as_of,
    gbp_replay_rows,
    market_was_closed,
    net_trade_cash,
    position_cost_basis_as_of,
    value_holdings,
)


class _FixedPriceSource:
    """A historical source with evidence for a fixed ``{ticker: price}`` set."""

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices
        self.calls: list[tuple[str, str]] = []

    def gbp_price(self, ticker: str, as_of: str) -> float | None:
        self.calls.append((ticker, as_of))
        return self._prices.get(ticker)

    def gbp_rate(self, currency: str, as_of: str) -> float | None:
        """Only GBP is evidenced -- these fixtures trade in sterling (#549)."""
        return 1.0 if currency.strip().upper() == "GBP" else None

    def trading_days(self, start: str, end: str) -> frozenset[str]:
        """No market calendar: the repair pass never asks for one."""
        return frozenset()


def _agent(tmp_path: Path) -> TraderAgent:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    return agent


def _service(
    agent: TraderAgent,
    source: object | None = None,
    estimate_unpriceable: bool = False,
) -> SnapshotRepairService:
    """Build the service; estimation is off by default (#519).

    These tests were written against the pre-#519 all-or-nothing rule and
    keep asserting it; the tests that opt into carrying-cost estimation pass
    ``estimate_unpriceable=True`` explicitly.
    """
    return SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        source,  # type: ignore[arg-type]
        estimate_unpriceable=estimate_unpriceable,
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

    # The nulled row is still offered to the source (evidence may have
    # arrived since), but leaving it NULL changes nothing, so it is
    # ``unchanged`` -- not counted as newly marked unavailable.
    assert (second.candidates, second.repaired, second.marked_unavailable) == (1, 0, 0)
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


def test_dry_run_with_evidence_reports_the_real_counts(tmp_path: Path) -> None:
    """A dry run's counts match the write pass exactly, but write nothing."""
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    service = _service(agent, _FixedPriceSource({"AAPL": 7.5}))

    dry = service.repair(dry_run=True)
    assert _values(agent, pf.id) == [0.0]

    wet = service.repair()

    assert dry.model_dump(exclude={"dry_run"}) == wet.model_dump(exclude={"dry_run"})
    assert (wet.repaired, wet.marked_unavailable) == (1, 0)
    assert _values(agent, pf.id) == [pytest.approx(75.0)]


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


def test_repair_cli_migrates_a_legacy_not_null_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standalone CLI must self-migrate rather than crash on a fresh DB.

    ``app/cli/repair_portfolio_snapshots.py`` connects directly, bypassing
    ``TraderAgent.model_post_init`` -- the only other place that has ever
    called ``init_trades_db``. Before this test's fix, running the CLI
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

    monkeypatch.setattr(repair_cli, "TRADES_DB", db_path)

    # --no-historical-evidence keeps this test off the real price cache.
    repair_cli.main(["--no-historical-evidence"])

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT total_value FROM portfolio_snapshots").fetchall() == [
        (None,)
    ]
    conn.close()


def _documented_invocation() -> list[str]:
    """Return the CLI's own documented command, parsed from its docstring.

    Parsing the usage line (rather than hard-coding it) is what stops the
    docstring and the real entry point drifting apart again: GH-480 was
    exactly that drift, a documented command nobody could run.
    """
    docstring = repair_cli.__doc__ or ""
    usage = next((line for line in docstring.splitlines() if "python -m" in line), "")
    assert usage, "the CLI docstring no longer documents a python -m invocation"
    words = shlex.split(usage.strip())
    return words[: words.index("-m") + 2]


def test_documented_invocation_runs_from_a_clean_shell() -> None:
    """``python -m app.cli.repair_portfolio_snapshots --help`` must work (#480).

    Run as a real subprocess with ``PYTHONPATH`` stripped, because
    ``pytest.ini``'s ``pythonpath = .`` masks the import failure in-process.
    ``--help`` is deliberate: argparse exits before any database is opened,
    so this never touches the developer's real ``trades.db``.
    """
    command = _documented_invocation()
    assert command[-1] == "app.cli.repair_portfolio_snapshots"

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, *command[command.index("-m") :], "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_null_row_is_reconstructed_when_evidence_arrives(tmp_path: Path) -> None:
    """A gap an earlier pass wrote is repaired once evidence exists (#481)."""
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    _service(agent, NoHistoricalPriceSource()).repair()
    assert _values(agent, pf.id) == [None]

    report = _service(agent, _FixedPriceSource({"AAPL": 7.5})).repair()

    assert (report.candidates, report.repaired, report.marked_unavailable) == (1, 1, 0)
    assert _values(agent, pf.id) == [pytest.approx(75.0)]


class _FakeBackfill:
    """A stand-in ``PriceEvidenceBackfillService`` recording every call."""

    def __init__(
        self,
        unavailable: set[str] | None = None,
        failing: set[str] | None = None,
        fx_unavailable: bool = False,
        fx_failing: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fx_calls: list[tuple[str, str]] = []
        self._unavailable = unavailable or set()
        self._failing = failing or set()
        self._fx_unavailable = fx_unavailable
        self._fx_failing = fx_failing

    def ensure_coverage(self, ticker: str, start, end) -> bool:
        self.calls.append((ticker, start.isoformat(), end.isoformat()))
        if ticker in self._unavailable:
            raise PriceEvidenceUnavailable(f"no rows for {ticker}")
        if ticker in self._failing:
            raise RuntimeError(f"transient failure for {ticker}")
        return True

    def ensure_fx_coverage(self, start, end) -> bool:
        self.fx_calls.append((start.isoformat(), end.isoformat()))
        if self._fx_unavailable:
            raise PriceEvidenceUnavailable("no FX rows")
        if self._fx_failing:
            raise RuntimeError("transient FX failure")
        return True


def test_prefetch_runs_once_per_ticker_across_two_portfolios(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("A")
    b = agent.create_portfolio("B")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=a.id)
    agent.record_buy("AAPL", 5, 5.0, "2024-01-10", portfolio_id=b.id)
    agent._snapshots.append(a.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    agent._snapshots.append(b.id, "2024-03-01T00:00:00+00:00", 0.0, 25.0, 100.0)
    backfill = _FakeBackfill()
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    service.repair()

    # One fetch total for the shared ticker, spanning the earliest trade
    # date across both portfolios through the latest candidate date (+1 day
    # exclusive), never one fetch per portfolio.
    assert backfill.calls == [("AAPL", "2024-01-01", "2024-03-02")]


def test_prefetch_skips_when_no_backfill_wired(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)

    report = _service(agent, NoHistoricalPriceSource()).repair()

    assert report.fetch_failures == ()
    assert report.newly_unavailable == ()


def test_prefetch_does_not_run_on_a_dry_run(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    backfill = _FakeBackfill()
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    service.repair(dry_run=True)

    assert backfill.calls == []


def test_one_tickers_definitive_failure_does_not_block_another(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("HSFWA", 3, 5.0, "2024-01-02", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    backfill = _FakeBackfill(unavailable={"HSFWA"})
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    report = service.repair()

    assert set(t for t, _, _ in backfill.calls) == {"AAPL", "HSFWA"}
    assert report.newly_unavailable == ("HSFWA",)
    assert report.fetch_failures == ()


def test_one_portfolios_broken_replay_does_not_abort_the_whole_prefetch(
    tmp_path: Path,
) -> None:
    """A crash while building one portfolio's span must not take out every
    other portfolio's evidence prefetch (#490). Scoped to ``_prefetch_evidence``
    itself -- the main reconstruction loop's own resilience to a broken
    portfolio replay is a separate, pre-existing concern this story doesn't
    touch."""
    agent = _agent(tmp_path)
    broken = agent.create_portfolio("BROKEN")
    healthy = agent.create_portfolio("HEALTHY")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=broken.id)
    agent.record_buy("MSFT", 3, 5.0, "2024-01-02", portfolio_id=healthy.id)
    agent._snapshots.append(broken.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    agent._snapshots.append(healthy.id, "2024-02-01T00:00:00+00:00", 0.0, 15.0, 100.0)
    rows = agent._snapshots.rows_with_ids()
    backfill = _FakeBackfill()

    real_open_rows = agent._trades.open_rows

    def flaky_open_rows(portfolio_id=None):
        if portfolio_id == broken.id:
            raise sqlite3.OperationalError("database is locked")
        return real_open_rows(portfolio_id)

    agent._trades.open_rows = flaky_open_rows  # type: ignore[method-assign]
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    fetch_failures, newly_unavailable = service._prefetch_evidence(
        rows
    )  # must not raise

    assert [t for t, _, _ in backfill.calls] == ["MSFT"]
    assert fetch_failures == ()
    assert newly_unavailable == ()


def test_transient_failure_is_reported_but_does_not_abort_the_run(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("MSFT", 3, 5.0, "2024-01-02", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    backfill = _FakeBackfill(failing={"MSFT"})
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    report = service.repair()

    assert set(t for t, _, _ in backfill.calls) == {"AAPL", "MSFT"}
    assert report.fetch_failures == ("MSFT",)
    assert report.newly_unavailable == ()


def test_prefetch_calls_ensure_fx_coverage_once_with_the_overall_span(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    a = agent.create_portfolio("A")
    b = agent.create_portfolio("B")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=a.id)
    agent.record_buy("MSFT", 5, 5.0, "2024-01-10", portfolio_id=b.id)
    agent._snapshots.append(a.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    agent._snapshots.append(b.id, "2024-03-01T00:00:00+00:00", 0.0, 25.0, 100.0)
    backfill = _FakeBackfill()
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    service.repair()

    # One shared FX fetch for the whole run, spanning the earliest span
    # start through the latest span end (+1 day, exclusive) across every
    # ticker -- never one fetch per ticker.
    assert backfill.fx_calls == [("2024-01-01", "2024-03-02")]


def test_prefetch_skips_fx_coverage_when_no_tickers_need_repair(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    # A cash-only 0.00 row is not a repair candidate, so `spans` stays empty.
    agent._snapshots.append(pf.id, "2024-01-01T00:00:00+00:00", 0.0, 0.0, 100.0)
    backfill = _FakeBackfill()
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    service.repair()

    assert backfill.fx_calls == []


def test_fx_definitive_failure_does_not_block_ticker_level_backfill(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    backfill = _FakeBackfill(fx_unavailable=True)
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    report = service.repair()

    assert [t for t, _, _ in backfill.calls] == ["AAPL"]
    assert backfill.fx_calls == [("2024-01-01", "2024-02-02")]
    assert report.newly_unavailable == (FX_PAIR,)
    assert report.fetch_failures == ()


def test_fx_transient_failure_is_isolated_from_ticker_backfill(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    backfill = _FakeBackfill(fx_failing=True)
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    report = service.repair()

    assert [t for t, _, _ in backfill.calls] == ["AAPL"]
    assert report.fetch_failures == (FX_PAIR,)
    assert report.newly_unavailable == ()


def test_a_tickers_failure_does_not_block_the_fx_fetch(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("HSFWA", 3, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 50.0, 100.0)
    backfill = _FakeBackfill(unavailable={"HSFWA"})
    service = SnapshotRepairService(
        agent._trades,
        agent._snapshots,
        NoHistoricalPriceSource(),
        backfill=backfill,  # type: ignore[arg-type]
    )

    report = service.repair()

    assert backfill.fx_calls == [("2024-01-01", "2024-02-02")]
    assert report.newly_unavailable == ("HSFWA",)


def test_null_row_without_evidence_stays_unchanged(tmp_path: Path) -> None:
    """A gap left as a gap is ``unchanged``, never re-``marked_unavailable``."""
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", None, 50.0, 100.0)

    report = _service(agent, NoHistoricalPriceSource()).repair()

    assert (report.repaired, report.marked_unavailable, report.unchanged) == (0, 0, 1)
    assert _values(agent, pf.id) == [None]


# --- #519: unpriceable holdings carried at cost, flagged estimated ---------


def _estimated_flags(agent: TraderAgent, portfolio_id: int) -> list[object]:
    return [row[4] for row in agent.snapshot_history(portfolio_id)]


def test_partial_evidence_values_the_gilt_at_carrying_cost(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent.record_buy("TR28", 100, 0.9, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", None, 140.0, 100.0)

    report = _service(
        agent, _FixedPriceSource({"AAPL": 7.5}), estimate_unpriceable=True
    ).repair()

    # 10 x 7.50 priced + 100 x 0.90 carried at cost.
    assert (report.repaired, report.estimated) == (1, 1)
    assert _values(agent, pf.id) == [pytest.approx(165.0)]
    assert _estimated_flags(agent, pf.id) == [1]


def test_no_evidence_at_all_falls_back_to_total_carrying_cost(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("TR28", 100, 0.9, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", None, 90.0, 100.0)

    report = _service(
        agent, NoHistoricalPriceSource(), estimate_unpriceable=True
    ).repair()

    assert (report.repaired, report.estimated) == (1, 1)
    assert _values(agent, pf.id) == [pytest.approx(90.0)]


def test_priced_holding_is_never_replaced_by_its_carrying_cost(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", None, 50.0, 100.0)

    report = _service(
        agent, _FixedPriceSource({"AAPL": 7.5}), estimate_unpriceable=True
    ).repair()

    assert (report.repaired, report.estimated) == (1, 0)
    assert _values(agent, pf.id) == [pytest.approx(75.0)]
    assert _estimated_flags(agent, pf.id) == [0]


def test_zero_carrying_cost_stays_unavailable(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    # A position whose carrying cost rounds to 0.00 (100 x 0.00004 = 0.004):
    # writing it back would recreate the very zero row this pass removes.
    agent.record_buy("TR28", 100, 0.00004, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 0.0, 100.0)

    report = _service(
        agent, NoHistoricalPriceSource(), estimate_unpriceable=True
    ).repair()

    assert (report.repaired, report.marked_unavailable) == (0, 1)
    assert _values(agent, pf.id) == [None]


def test_estimated_repair_is_idempotent(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("TR28", 100, 0.9, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", None, 90.0, 100.0)
    service = _service(agent, NoHistoricalPriceSource(), estimate_unpriceable=True)

    service.repair()
    second = service.repair()

    # The row now carries a real (estimated) value, so it is no longer a
    # candidate at all: the second pass reports it unchanged.
    assert (second.candidates, second.repaired, second.unchanged) == (0, 0, 1)
    assert _values(agent, pf.id) == [pytest.approx(90.0)]


def test_estimation_disabled_keeps_the_honest_gap(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("TR28", 100, 0.9, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 90.0, 100.0)

    report = _service(agent, NoHistoricalPriceSource()).repair()

    assert (report.repaired, report.estimated, report.marked_unavailable) == (0, 0, 1)
    assert _values(agent, pf.id) == [None]


def test_repair_cli_no_historical_evidence_estimates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-historical-evidence`` still nulls every candidate row (#519)."""
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("TR28", 100, 0.9, "2024-01-01", portfolio_id=pf.id)
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 90.0, 100.0)
    monkeypatch.setattr(repair_cli, "TRADES_DB", agent.db_path)

    repair_cli.main(["--no-historical-evidence"])

    assert _values(agent, pf.id) == [None]
    assert _estimated_flags(agent, pf.id) == [0]


def test_legacy_database_gains_the_estimated_column_idempotently(
    tmp_path: Path,
) -> None:
    """A pre-#519 table shape gains ``value_is_estimated`` exactly once."""
    conn = sqlite3.connect(tmp_path / "trades.db")
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
    db.init_trades_db(conn)
    conn.commit()

    columns = [row[1] for row in conn.execute("PRAGMA table_info(portfolio_snapshots)")]
    assert columns.count("value_is_estimated") == 1
    assert conn.execute(
        "SELECT id, total_value, total_cost, cash_balance, value_is_estimated "
        "FROM portfolio_snapshots"
    ).fetchall() == [(7, 100.0, 90.0, 10.0, 0)]
    conn.close()


# --- GH-549: replay prices are converted to GBP -----------------------------


def _replay_row(
    ticker: str,
    action: str,
    shares: float,
    price: float,
    when: str,
    currency: str = "GBP",
) -> tuple[object, ...]:
    """One repository replay tuple, currency in the 8th column (#549)."""
    return (ticker, action, shares, price, when, None, None, currency)


def _rate(currency: str, as_of: str) -> float | None:
    """9.8 HKD per GBP on the one evidenced day; nothing else is known."""
    return 9.8 if (currency, as_of) == ("HKD", "2024-01-01") else None


def test_gbp_rows_are_returned_untouched(tmp_path: Path) -> None:
    """An all-GBP portfolio must be byte-identical to before #549 -- and
    must not cost an FX lookup either."""
    rows = [_replay_row("AAPL", "BUY", 10, 5.0, "2024-01-01")]

    def boom(currency: str, as_of: str) -> float | None:
        raise AssertionError("a GBP row must never ask for a rate")

    assert gbp_replay_rows(rows, boom) == rows


def test_a_foreign_price_is_divided_by_its_dated_rate() -> None:
    converted = gbp_replay_rows(
        [_replay_row("9988", "BUY", 10, 80.0, "2024-01-01", "HKD")], _rate
    )
    # Only the price changes; every other column survives untouched.
    assert converted[0][3] == pytest.approx(80.0 / 9.8)
    assert converted[0][:3] == ("9988", "BUY", 10)
    assert converted[0][4:] == ("2024-01-01", None, None, "HKD")


def test_a_rate_is_looked_up_once_per_currency_and_date() -> None:
    calls: list[tuple[str, str]] = []

    def counting(currency: str, as_of: str) -> float | None:
        calls.append((currency, as_of))
        return _rate(currency, as_of)

    gbp_replay_rows(
        [
            _replay_row("9988", "BUY", 10, 80.0, "2024-01-01", "HKD"),
            _replay_row("0700", "BUY", 5, 300.0, "2024-01-01", "HKD"),
            _replay_row("9988", "SELL", 2, 90.0, "2024-01-02", "HKD"),
        ],
        counting,
    )
    assert calls == [("HKD", "2024-01-01"), ("HKD", "2024-01-02")]


def test_a_foreign_price_with_no_dated_rate_becomes_none() -> None:
    """The exact-date contract, not a nearest-prior guess: the day after the
    evidenced one is unconvertible, so its price is unavailable."""
    converted = gbp_replay_rows(
        [_replay_row("9988", "BUY", 10, 80.0, "2024-01-02", "HKD")], _rate
    )
    assert converted[0][3] is None


def test_cost_basis_uses_the_converted_price() -> None:
    rows = gbp_replay_rows(
        [_replay_row("9988", "BUY", 10, 80.0, "2024-01-01", "HKD")], _rate
    )
    assert position_cost_basis_as_of(rows, "2024-02-01") == {
        "9988": pytest.approx(800.0 / 9.8)
    }
    assert cost_basis_as_of(rows, "2024-02-01") == pytest.approx(81.63)


def test_an_unconvertible_trade_makes_its_cost_unavailable() -> None:
    """A GBP-assumed 800.0 would carry the position at ~10x its worth, so
    the answer is ``None`` for that ticker and for the whole total."""
    rows = gbp_replay_rows(
        [
            _replay_row("9988", "BUY", 10, 80.0, "2024-01-02", "HKD"),
            _replay_row("AAPL", "BUY", 10, 5.0, "2024-01-02"),
        ],
        _rate,
    )
    assert position_cost_basis_as_of(rows, "2024-02-01") == {
        "9988": None,
        "AAPL": pytest.approx(50.0),
    }
    assert cost_basis_as_of(rows, "2024-02-01") is None


def test_net_trade_cash_is_none_when_a_trade_cannot_be_converted() -> None:
    rows = gbp_replay_rows(
        [_replay_row("9988", "BUY", 10, 80.0, "2024-01-02", "HKD")], _rate
    )
    assert net_trade_cash(rows, "2024-01-01", "2024-01-31") is None
    # Outside the interval it never contributes, so the answer is real again.
    assert net_trade_cash(rows, "2024-02-01", "2024-02-28") == 0.0


def test_net_trade_cash_falls_by_a_converted_buy() -> None:
    rows = gbp_replay_rows(
        [_replay_row("9988", "BUY", 10, 80.0, "2024-01-01", "HKD")], _rate
    )
    assert net_trade_cash(rows, "2023-12-31", "2024-01-31") == pytest.approx(
        -800.0 / 9.8
    )


def test_an_unavailable_carrying_cost_is_not_an_estimate() -> None:
    """A ``None`` cost is exactly as absent as a missing one: the point is
    unavailable rather than estimated from an unconverted figure."""
    source = _FixedPriceSource({})
    assert value_holdings(source, {"9988": 10.0}, "2024-02-01", {"9988": None}) == (
        None,
        False,
    )


def test_repair_leaves_an_unconvertible_holding_unavailable(tmp_path: Path) -> None:
    """End to end: an HKD holding with no dated rate and no dated close is
    an honest gap, never a ~10x-overstated carrying cost (#549)."""
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("9988", 10, 80.0, "2024-01-01", portfolio_id=pf.id)
    agent.save_ticker_currencies({"9988": "HKD"})
    agent._snapshots.append(pf.id, "2024-02-01T00:00:00+00:00", 0.0, 800.0, 100.0)

    report = _service(agent, _FixedPriceSource({}), estimate_unpriceable=True).repair()

    assert (report.repaired, report.marked_unavailable) == (0, 1)
    assert _values(agent, pf.id) == [None]


def test_an_unconvertible_sell_leaves_the_cost_basis_intact() -> None:
    """A sell price never enters an average cost, so a missing rate on a
    sell day costs the replay nothing (#549 review)."""
    rows = gbp_replay_rows(
        [
            _replay_row("9988", "BUY", 10, 80.0, "2024-01-01", "HKD"),
            _replay_row("9988", "SELL", 2, 90.0, "2024-01-02", "HKD"),
        ],
        _rate,
    )
    assert position_cost_basis_as_of(rows, "2024-02-01") == {
        "9988": pytest.approx(8 * 80.0 / 9.8)
    }


def test_closing_a_position_clears_its_unconvertible_mark() -> None:
    """A reopened position is judged on its own trades, not on a closed
    lot's missing FX rate (#549 review)."""

    def rate(currency: str, as_of: str) -> float | None:
        """Unevidenced on the opening buy's day, evidenced on the reopen's."""
        return 9.8 if (currency, as_of) == ("HKD", "2024-01-04") else None

    rows = gbp_replay_rows(
        [
            _replay_row("9988", "BUY", 10, 80.0, "2024-01-02", "HKD"),
            _replay_row("9988", "SELL", 10, 90.0, "2024-01-03", "HKD"),
            _replay_row("9988", "BUY", 5, 80.0, "2024-01-04", "HKD"),
        ],
        rate,
    )
    assert position_cost_basis_as_of(rows, "2024-02-01") == {
        "9988": pytest.approx(5 * 80.0 / 9.8)
    }


# --- GH-550: weekends are closed unconditionally ----------------------------

#: 2024-01-01..05 are Mon-Fri, 06 is a Saturday and 07 a Sunday.
_TRADING_WEEK = frozenset(
    {"2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"}
)


@pytest.mark.parametrize("weekend_day", ["2024-01-06", "2024-01-07"])
def test_a_weekend_is_closed_even_when_a_holding_reports_a_price(
    weekend_day: str,
) -> None:
    """A stray weekend NAV must not defeat the closure test (#550).

    No market the app tracks opens at the weekend, so a Saturday close is
    wrong evidence rather than a reason to doubt the calendar.
    """
    source = _FixedPriceSource({"AAPL": 7.5})

    assert market_was_closed(source, {"AAPL": 10.0}, weekend_day, _TRADING_WEEK)


def test_a_weekend_is_closed_without_consulting_the_calendar_or_prices() -> None:
    """Neither piece of evidence is needed, so neither is read (#550)."""
    source = _FixedPriceSource({"AAPL": 7.5})

    assert market_was_closed(source, {"AAPL": 10.0}, "2024-01-06", frozenset())
    assert source.calls == []


def test_a_weekend_with_no_holdings_is_not_closed() -> None:
    """An empty portfolio has nothing to carry forward (unchanged)."""
    assert not market_was_closed(_FixedPriceSource({}), {}, "2024-01-06", _TRADING_WEEK)


def test_a_weekday_on_the_calendar_is_open() -> None:
    assert not market_was_closed(
        _FixedPriceSource({"AAPL": 7.5}), {"AAPL": 10.0}, "2024-01-03", _TRADING_WEEK
    )


def test_a_weekday_off_the_calendar_with_nothing_priced_is_closed() -> None:
    """The two-condition bank-holiday test is untouched by #550."""
    assert market_was_closed(
        _FixedPriceSource({}),
        {"AAPL": 10.0},
        "2024-01-03",
        _TRADING_WEEK - {"2024-01-03"},
    )


def test_a_weekday_off_the_calendar_with_a_price_is_still_open() -> None:
    """One priced holding on a weekday keeps the day a valuation, not a
    closure -- the #519 estimated flag has to stay reachable."""
    assert not market_was_closed(
        _FixedPriceSource({"AAPL": 7.5}),
        {"AAPL": 10.0},
        "2024-01-03",
        _TRADING_WEEK - {"2024-01-03"},
    )


def test_an_unknown_calendar_still_means_cannot_tell_on_a_weekday() -> None:
    assert not market_was_closed(
        _FixedPriceSource({}), {"AAPL": 10.0}, "2024-01-03", frozenset()
    )


def test_a_malformed_date_falls_through_to_the_weekday_test() -> None:
    """A backfill must never abort on one unparseable timestamp (#550)."""
    assert market_was_closed(
        _FixedPriceSource({}), {"AAPL": 10.0}, "not-a-date", _TRADING_WEEK
    )


def test_repair_leaves_a_weekend_row_alone(tmp_path: Path) -> None:
    """A Saturday has no session to revalue, so the pass must not price it
    off a stray weekend close (#550 review)."""
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-02", portfolio_id=pf.id)
    # 2024-01-06 is a Saturday, and the source has a (stale) close for it.
    agent._snapshots.append(pf.id, "2024-01-06T00:00:00+00:00", None, 50.0, 100.0)

    report = _service(
        agent, _FixedPriceSource({"AAPL": 9.0}), estimate_unpriceable=True
    ).repair()

    assert (report.candidates, report.repaired) == (0, 0)
    assert _values(agent, pf.id) == [None]


def test_a_stored_timestamp_is_classified_as_a_weekend() -> None:
    """``market_was_closed`` reads the date part, so a snapshot key answers
    the same as a bare day (#550 review)."""
    source = _FixedPriceSource({"AAPL": 9.0})
    assert market_was_closed(
        source, {"AAPL": 1.0}, "2024-01-06T00:00:00+00:00", frozenset()
    )
