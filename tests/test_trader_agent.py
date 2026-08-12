import sqlite3
from pathlib import Path

import pytest

from app.agents.trader.trader_agent import (
    SippImportError,
    TraderAgent,
    _idempotency_key,
    _to_iso_date,
)
from app.core.config import PORTFOLIO_VALUE_CSV
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.trades_repo import TradesRepository


def test_record_multiple_buys(tmp_path: Path) -> None:
    db_path = tmp_path / "trades.db"
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = db_path
    agent._init_db()

    agent.record_buy("TEST1", 1.0, 150.0, "2026-04-30")
    agent.record_buy("TEST2", 2.0, 300.0, "2026-04-30")

    portfolio = agent.get_portfolio()
    assert {position.ticker for position in portfolio} == {"TEST1", "TEST2"}
    assert sum(position.shares for position in portfolio) == 3.0


def test_set_unmatched_sell_ack_persists_and_clears(tmp_path: Path) -> None:
    """Story 1.5, AC #7: ``set_unmatched_sell_ack`` writes a non-null ISO
    timestamp when acknowledged, and clears it back to ``None`` when
    un-acknowledged -- read back through the real repository/DB."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    sell = agent.record_sell("TEST1", 5, 100.0, "2026-01-01")
    assert sell.id is not None

    agent.set_unmatched_sell_ack(sell.id, True)
    history = {t.id: t for t in agent.get_trade_history()}
    assert history[sell.id].realised_pnl_ack_at is not None

    agent.set_unmatched_sell_ack(sell.id, False)
    history = {t.id: t for t in agent.get_trade_history()}
    assert history[sell.id].realised_pnl_ack_at is None


def test_get_and_save_cached_fx_rates_round_trip(tmp_path: Path) -> None:
    """Story 1.2: TraderAgent's get/save_cached_fx_rates wiring persists
    through a real FxRateCacheRepository against a real DB."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    assert agent.get_cached_fx_rates(["2026-01-01"]) == {}

    agent.save_fx_rates({"2026-01-01": 1.35, "2026-01-02": 1.36})
    result = agent.get_cached_fx_rates(["2026-01-01", "2026-01-02", "2026-01-03"])
    assert result == {"2026-01-01": 1.35, "2026-01-02": 1.36}


def test_correct_latest_trade(tmp_path: Path) -> None:
    db_path = tmp_path / "trades.db"
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = db_path
    agent._init_db()

    agent.record_buy(
        "TEST1",
        5.0,
        100.0,
        "2026-04-30",
        notes="Initial buy",
        stop_loss=90.0,
        entry_price=100.0,
    )
    corrected = agent.correct_trade(
        "TEST1",
        4.0,
        105.0,
        "2026-05-01",
        notes="Updated quantity and price",
        stop_loss=92.0,
        entry_price=105.0,
    )

    assert corrected.shares == 4.0
    assert corrected.price == 105.0
    assert corrected.stop_loss == 92.0
    assert corrected.entry_price == 105.0

    latest = agent.get_latest_trade("TEST1")
    assert latest is not None
    assert latest.shares == 4.0
    assert latest.price == 105.0
    assert latest.notes == "Updated quantity and price"


def test_import_sipp_is_idempotent(tmp_path: Path) -> None:
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    agent.import_sipp(csv_path.read_bytes())
    agent.import_sipp(csv_path.read_bytes())  # re-import must not duplicate

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].ticker == "AAPL"
    assert portfolio[0].shares == 10.0  # not 20.0


def test_sipp_reports_inserted_then_duplicate_outcomes(tmp_path: Path) -> None:
    """Re-importing the same CSV reports duplicates, not a second success.

    Story 1.8, AC #2/#4: ``buy_count`` counts only rows that genuinely
    inserted, so a row suppressed by the unique index shows up as a
    duplicate instead of being folded into the success count.
    """
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-A,1000.00,,5000.00\n"
        "02/02/2024,MSFT,B456,5,200.00,Buy MSFT,REF-B,1000.00,,5000.00\n"
    )
    csv_bytes = csv_text.encode("utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    first = agent.import_sipp(csv_bytes)
    second = agent.import_sipp(csv_bytes)

    assert (first.inserted_count, first.duplicate_count, first.buy_count) == (2, 0, 2)
    assert (second.inserted_count, second.duplicate_count, second.buy_count) == (
        0,
        2,
        0,
    )
    with sqlite3.connect(agent.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


def test_sipp_rejected_plan_reports_four_outcomes_without_writing(
    tmp_path: Path,
) -> None:
    """Story 1.8, AC #5: a rejected plan still reports all four outcomes.

    The inserted/duplicate counts come from the read-only pre-check (no
    write is ever attempted), and they still reconcile against total_rows.
    """
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-A,1000.00,,5000.00\n"
        "02/02/2024,MSFT,B456,not-a-number,200.00,Buy MSFT,REF-B,1000.00,,5000.00\n"
        ",,,,,,,,,\n"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode("utf-8"))

    assert result.status == "rejected"
    assert (result.inserted_count, result.duplicate_count, result.skipped_count) == (
        1,
        0,
        1,
    )
    assert len(result.failed_rows) == 1
    assert result.total_rows == 3
    assert (
        result.inserted_count
        + result.duplicate_count
        + result.skipped_count
        + len(result.failed_rows)
        == result.total_rows
    )
    with sqlite3.connect(agent.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_sipp_mixed_plan_counts_all_four_outcomes_simultaneously(
    tmp_path: Path,
) -> None:
    """Story 1.8, AC #1: one CSV holding one row of each outcome.

    Each row resolves to exactly one of inserted/duplicate/skipped/failed,
    and the four reconcile against total_rows. The failed row rejects the
    whole plan, so the inserted/duplicate counts here describe nothing that
    was persisted — proved by reading the tables directly rather than
    trusting the same counters under test.
    """
    header = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
    )
    already_imported = (
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-A,1000.00,,5000.00\n"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp((header + already_imported).encode("utf-8"))

    mixed = (
        header
        # duplicate -- byte-identical to the row imported above
        + already_imported
        # inserted -- genuinely new
        + "03/02/2024,MSFT,B456,5,200.00,Buy MSFT,REF-B,1000.00,,4000.00\n"
        # skipped -- no quantity, no debit/credit, nothing actionable
        + "04/02/2024,n/a,n/a,n/a,n/a,Statement note,n/a,,,4000.00\n"
        # failed -- unparseable quantity
        + "05/02/2024,TSLA,B789,notanumber,300.00,Buy TSLA,REF-D,900.00,,3100.00\n"
    )

    result = agent.import_sipp(mixed.encode("utf-8"))

    assert result.status == "rejected"
    assert result.inserted_count == 1
    assert result.duplicate_count == 1
    assert result.skipped_count == 1
    assert len(result.failed_rows) == 1
    assert result.total_rows == 4
    assert (
        result.inserted_count
        + result.duplicate_count
        + result.skipped_count
        + len(result.failed_rows)
        == result.total_rows
    )
    with sqlite3.connect(agent.db_path) as conn:
        tickers = [r[0] for r in conn.execute("SELECT ticker FROM trades").fetchall()]
    # Only the first import's row survives: the rejected plan wrote nothing,
    # so the "inserted" MSFT row never landed.
    assert tickers == ["AAPL"]


def test_idempotency_key_is_content_based_and_excludes_reference() -> None:
    key = _idempotency_key("2024-02-01", "AAPL", "B123", "10", "Buy AAPL")
    assert key == _idempotency_key("2024-02-01", "AAPL", "B123", "10", "Buy AAPL")
    assert key != _idempotency_key("2024-02-02", "AAPL", "B123", "10", "Buy AAPL")


def test_overlapping_sipp_files_dedupe_without_reference(tmp_path: Path) -> None:
    header = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
    )
    first = header + "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,,1000.00,,5000.00\n"
    second = header + "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,N/A,1000.00,,5000.00\n"
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    agent.import_sipp(first.encode("utf-8"))
    result = agent.import_sipp(second.encode("utf-8"))

    # The second file's row is the same content with a different Reference --
    # the content-based key still recognises it as an already-imported row.
    assert (result.inserted_count, result.duplicate_count) == (0, 1)
    with sqlite3.connect(agent.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_reference_no_reference_casing_is_normalized(tmp_path: Path) -> None:
    header = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
    )
    rows = "".join(
        f"01/02/2024,AAPL,B123,10,100.00,Buy AAPL,{ref},1000.00,,5000.00\n"
        for ref in ("N/A", "n/a")
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp((header + rows).encode("utf-8"))

    # Two identical rows within one file: the second is a duplicate, and the
    # read-only pre-check must agree with what INSERT OR IGNORE actually did.
    assert (result.inserted_count, result.duplicate_count) == (1, 1)
    with sqlite3.connect(agent.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_import_sipp_handles_stacked_bom_in_first_header(tmp_path: Path) -> None:
    # Some provider exports prepend a run of BOM chars to the first header cell
    # ("﻿...﻿Date"); utf-8-sig strips only one, so the Date column would
    # be missed and every trade date stored blank (#166). The import must still
    # read the date.
    bom = "﻿" * 40
    csv_text = (
        f"{bom}Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,"
        "Credit,Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path.read_bytes())

    trades = agent.get_trade_history()
    assert len(trades) == 1
    assert trades[0].ticker == "AAPL"
    assert trades[0].date == "2024-02-01"  # not blank


def test_import_sipp_handles_mojibake_bom_in_first_header(tmp_path: Path) -> None:
    # Some export pipelines decode a BOM as Latin-1 and re-save as UTF-8,
    # turning it into the 3-character mojibake "ï»¿" stacked ahead of the
    # first header cell ("ï»¿...ï»¿Date"). This is a different byte pattern
    # from the real BOM character (U+FEFF) handled above and previously
    # slipped past the header cleanup, causing the Date column to be
    # reported as missing and the import rejected outright (#171).
    bom = "ï»¿" * 25
    csv_text = (
        f"{bom}Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,"
        "Credit,Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path.read_bytes())

    trades = agent.get_trade_history()
    assert len(trades) == 1
    assert trades[0].ticker == "AAPL"
    assert trades[0].date == "2024-02-01"  # not blank, import not rejected


def test_to_iso_date_strips_embedded_bom() -> None:
    # Some exports embed BOM chars mid-value (e.g. "12/10/2﻿﻿020"); the
    # date must still parse rather than being stored as a polluted string (#166).
    assert _to_iso_date("12/10/2﻿﻿﻿020") == "2020-10-12"
    assert _to_iso_date("﻿2024-02-01") == "2024-02-01"


def test_oversell_is_logged_and_clamped(tmp_path: Path, caplog) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("TEST1", 10.0, 100.0, "2026-04-30")
    agent.record_sell("TEST1", 25.0, 110.0, "2026-05-01")  # oversell

    import logging

    with caplog.at_level(logging.WARNING):
        portfolio = agent.get_portfolio()

    # Position is gone (clamped to 0), and the oversell was logged
    assert all(p.ticker != "TEST1" for p in portfolio)
    assert any("Oversell" in r.message for r in caplog.records)


def test_import_sipp_clean_well_formed_import_commits_and_closes_connection(
    tmp_path: Path,
) -> None:
    # Smoke test only -- this does NOT exercise rollback (a clean,
    # well-formed import commits successfully and releases the connection).
    # See test_import_sipp_mid_commit_failure_rolls_back_everything and
    # test_import_sipp_commit_failure_before_any_write_also_rolls_back below
    # for the actual rollback-on-failure regression coverage (Story 1.2,
    # Gate 7).
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,10,100.00,Buy,REF1,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path.read_bytes())
    assert len(agent.get_portfolio()) == 1


def _table_counts(db_path: Path, portfolio_id: int) -> dict[str, int]:
    """Raw-SQL row counts across all four of Story 1.2's atomic-commit
    targets, scoped to one portfolio. Direct DB inspection (not
    ``agent.get_portfolio()``) is required to prove AC #1/#3 -- a
    portfolio-view check alone would not observe cash-balance/snapshot
    state left behind by a partial commit."""
    conn = sqlite3.connect(db_path)
    try:
        trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone()[0]
        cash_flows = conn.execute(
            "SELECT COUNT(*) FROM cash_flows WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone()[0]
        cash_balance_key = conn.execute(
            "SELECT COUNT(*) FROM account_state WHERE key = ?",
            (f"cash_balance:{portfolio_id}",),
        ).fetchone()[0]
        snapshots = conn.execute(
            "SELECT COUNT(*) FROM portfolio_snapshots WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "trades": trades,
        "cash_flows": cash_flows,
        "account_state": cash_balance_key,
        "portfolio_snapshots": snapshots,
    }


def test_import_sipp_rejected_plan_leaves_zero_rows_in_all_four_tables(
    tmp_path: Path,
) -> None:
    """Story 1.2, AC #1/#3: a plan with several otherwise-good rows (a
    trade and a cash flow) plus one bad row leaves zero rows in every one
    of the four atomic-commit targets -- inspected directly via raw SQL,
    not just ``agent.get_portfolio()``, which would not observe cash
    balance or snapshot state."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,,,,Contribution,REF-C,,1000.00,1000.00\n"
        "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
        "03/01/2024,MSFT,B2,notanumber,100.00,Buy MSFT,REF-BAD,1000.00,,1500.00\n"
    )

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id)

    assert result.status == "rejected"
    assert len(result.failed_rows) == 1
    counts = _table_counts(agent.db_path, pf.id)
    assert counts == {
        "trades": 0,
        "cash_flows": 0,
        "account_state": 0,
        "portfolio_snapshots": 0,
    }
    assert agent.get_portfolio(portfolio_id=pf.id) == []


def test_import_sipp_mid_commit_failure_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story 1.2, AC #3: inject a failure inside the commit phase *after*
    trades/cash-flows/account-state would already have been written to the
    open (uncommitted) connection -- proving the rollback undoes
    already-executed-but-uncommitted statements, not just prevents later
    ones from starting. The snapshot append (the last write in the commit
    phase) is monkeypatched to raise."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom: snapshot append failed")

    monkeypatch.setattr(PortfolioSnapshotsRepository, "append_on_connection", _boom)

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,,,,Contribution,REF-C,,1000.00,1000.00\n"
        "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    with pytest.raises(RuntimeError, match="boom"):
        agent.import_sipp(csv_text.encode("utf-8"), pf.id)

    counts = _table_counts(agent.db_path, pf.id)
    assert counts == {
        "trades": 0,
        "cash_flows": 0,
        "account_state": 0,
        "portfolio_snapshots": 0,
    }
    assert agent.get_portfolio(portfolio_id=pf.id) == []


def test_import_sipp_commit_failure_before_any_write_also_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to the mid-commit-failure test above: a failure on the
    very *first* write inside the commit phase (before cash-flows,
    account-state, or the snapshot are ever touched) also leaves nothing
    durable -- both ends of the transaction are covered."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom: first trade insert failed")

    monkeypatch.setattr(TradesRepository, "insert_ignore", _boom)

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    with pytest.raises(RuntimeError, match="boom"):
        agent.import_sipp(csv_text.encode("utf-8"), pf.id)

    counts = _table_counts(agent.db_path, pf.id)
    assert counts == {
        "trades": 0,
        "cash_flows": 0,
        "account_state": 0,
        "portfolio_snapshots": 0,
    }


def test_import_sipp_reports_every_failed_row_with_distinguishable_reason(
    tmp_path: Path,
) -> None:
    """Story 1.2, AC #4: a plan with two or more distinct bad rows reports
    every one of them in ``failed_rows``, each with a distinguishable
    reason -- not just the first one encountered."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,notanumber,100.00,Buy AAPL,REF-BAD-QTY,1000.00,,5000.00\n"
        "02/02/2024,MSFT,B2,5,-100.00,Buy MSFT,REF-BAD-PRICE,1000.00,,4000.00\n"
    )

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id)

    assert result.status == "rejected"
    assert len(result.failed_rows) == 2
    joined = " | ".join(result.failed_rows)
    assert "REF-BAD-QTY" in joined and "unparseable quantity" in joined
    assert "REF-BAD-PRICE" in joined and "non-positive" in joined


def test_replay_orders_by_trade_date_not_file_order(tmp_path: Path) -> None:
    # Two CSV rows: later date first (file order), earlier date second.
    # Correct chronological replay = BUY 10 @ 2024-01-01, then SELL 4 @ 2024-02-01 => 6 shares.
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,4,110.00,Sell AAPL,REF-SELL,, 440.00,4560.00\n"
        "01/01/2024,AAPL,B1,10,100.00,Buy AAPL,REF-BUY,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path.read_bytes())

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    pos = portfolio[0]
    assert pos.shares == 6.0
    assert pos.entry_date == "2024-01-01"  # ISO date stored from DD/MM/YYYY input


def test_replay_correct_with_mixed_date_formats(tmp_path: Path) -> None:
    # Manual BUY (ISO date) is earlier; imported SELL (DD/MM/YYYY) is later.
    # Correct chronological replay = BUY 10 then SELL 4 => 6 shares.
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("AAPL", 10.0, 100.0, "2024-01-01")

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,4,110.00,Sell AAPL,REF-S1,,440.00,4560.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent.import_sipp(csv_path.read_bytes())

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].shares == 6.0
    assert portfolio[0].entry_date == "2024-01-01"


def test_to_iso_date_converts_known_formats() -> None:
    assert _to_iso_date("01/02/2024") == "2024-02-01"
    assert _to_iso_date("2024-02-01") == "2024-02-01"
    assert _to_iso_date("  2024-03-15  ") == "2024-03-15"


def test_sipp_classifies_cash_flows(tmp_path: Path) -> None:
    import sqlite3

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,,,,Monthly contribution,REF-C1,,500.00,500.00\n"
        "02/01/2024,n/a,,,,Tax relief,REF-T1,,125.00,625.00\n"
        "03/01/2024,n/a,,,,AAPL dividend,REF-D1,,12.50,637.50\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path.read_bytes())

    conn = sqlite3.connect(agent.db_path)
    rows = conn.execute(
        "SELECT flow_type, amount FROM cash_flows ORDER BY id"
    ).fetchall()
    conn.close()

    assert rows == [
        ("CONTRIBUTION", 500.0),
        ("TAX_RELIEF", 125.0),
        ("DIVIDEND", 12.5),
    ]
    # None of these created phantom trade positions
    assert agent.get_portfolio() == []


def test_sipp_parses_eur_and_usd_amounts(tmp_path: Path) -> None:
    """EUR/USD-denominated cash-flow rows must parse, not just GBP (#186) —
    a real SIPP export can carry a EUR interest sub-account, and the
    currency symbol was previously only stripped for £."""
    import sqlite3

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "28/05/2024,n/a,n/a,n/a,n/a,Gross interest to 24/05/24,n/a,n/a,"
        "€0.38,€153.61\n"
        "29/05/2024,n/a,n/a,n/a,n/a,US dividend,n/a,n/a,$1.25,$154.86\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())

    assert result.parse_errors == []
    assert result.skipped_rows == []
    conn = sqlite3.connect(agent.db_path)
    rows = conn.execute(
        "SELECT flow_type, amount FROM cash_flows ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [("INTEREST", 0.38), ("DIVIDEND", 1.25)]
    assert result.cash_balance == 154.86


def test_sipp_imports_dividend_row_that_has_symbol_but_no_quantity(
    tmp_path: Path,
) -> None:
    """A cash dividend tagged with the paying company's Symbol/Sedol but no
    Quantity (no shares changed hands) must still be imported as a cash
    flow, not silently dropped — this shape is real provider output (#186)."""
    import sqlite3

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "06/06/2023,LIGHT,BYY7VY5,n/a,n/a,"
        "Div 60   SIGNIFY NV   EUR0.01,n/a,n/a,76.50,150.45\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())

    assert result.buy_count == 0 and result.sell_count == 0
    conn = sqlite3.connect(agent.db_path)
    rows = conn.execute(
        "SELECT flow_type, amount FROM cash_flows ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [("DIVIDEND", 76.50)]
    # No phantom LIGHT position was created from the Symbol column.
    assert agent.get_portfolio() == []


def test_sipp_issue_detail_falls_back_to_csv_row_when_reference_is_na(
    tmp_path: Path,
) -> None:
    """When Reference is blank/"n/a" (common for interest/dividend rows),
    issue detail must still point at a specific row instead of a useless
    "row n/a" repeated for every failure (#186)."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Gross interest,n/a,n/a,not-a-number,100.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())

    assert len(result.parse_errors) == 1
    # Row 1 is the header, so the first data row is CSV row 2.
    assert "CSV row 2" in result.parse_errors[0]
    assert "n/a" not in result.parse_errors[0].split(":")[0]


def test_sipp_cash_balance_is_final_running_balance(tmp_path: Path) -> None:
    # Rows in chronological (oldest-first) order, as the documented import
    # expects. The returned cash balance is the last running balance.
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,,,,Contribution,REF-C1,,1000.00,1000.00\n"
        "01/02/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B1,500.00,,500.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())
    assert result.cash_balance == 500.0


def test_sipp_total_rows_and_status_ok_when_every_row_is_accounted_for(
    tmp_path: Path,
) -> None:
    """``total_rows`` tracks the data-row count and ``status`` stays "ok"
    when buy/sell/cash/skipped counts add up to it (#187)."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,,,,Contribution,REF-C1,,1000.00,1000.00\n"
        "01/02/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B1,500.00,,500.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())

    assert result.total_rows == 2
    assert result.buy_count + result.cash_flow_count == result.total_rows
    assert result.status == "ok"


def test_sipp_unparseable_cash_amount_rejects_the_whole_plan(
    tmp_path: Path,
) -> None:
    """A non-trade row whose Debit/Credit is unparseable lands at amount=0
    with no Quantity/Symbol to make it a trade -- this row cannot be safely
    written, so under the all-or-nothing commit rule (Story 1.2, AC #2) the
    entire plan is rejected, not silently skipped-but-still-committed
    (#187, #210)."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Gross interest,n/a,not-a-number,,100.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())

    assert result.total_rows == 1
    assert result.buy_count == 0
    assert result.sell_count == 0
    assert result.cash_flow_count == 0
    assert result.skipped_rows == []
    assert len(result.failed_rows) == 1
    assert "unparseable cash amount" in result.failed_rows[0]
    assert result.status == "rejected"


def test_sipp_benign_empty_row_does_not_flag_status_error(tmp_path: Path) -> None:
    """A row with no quantity and no debit/credit (e.g. a pure informational
    line) is genuinely a no-op, not a problem — it must still count toward
    the row-count reconciliation so ``status`` stays "ok", but must not be
    reported to the user as a skipped/problem row (#187)."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Statement note,n/a,,,100.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())

    assert result.total_rows == 1
    assert result.status == "ok"
    # The FR-13 "skipped" outcome is its own count, populated from the benign
    # no-signal rows. The vestigial skipped_rows list is a different thing
    # and stays empty -- wiring skipped_count to it would be wrong.
    assert result.skipped_count == 1
    assert result.skipped_rows == []


def test_record_buy_normalizes_ddmmyyyy_to_iso(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("AAPL", 5.0, 100.0, "15/03/2024")

    latest = agent.get_latest_trade("AAPL")
    assert latest is not None
    assert latest.date == "2024-03-15"  # stored ISO, not "15/03/2024"


def test_replay_correct_when_record_buy_uses_ddmmyyyy(tmp_path: Path) -> None:
    # A DD/MM/YYYY manual BUY (earlier) and an ISO manual SELL (later) for the
    # same ticker must replay chronologically: BUY 10 then SELL 4 => 6 shares.
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("AAPL", 10.0, 100.0, "01/02/2024")  # -> 2024-02-01
    agent.record_sell("AAPL", 4.0, 110.0, "2024-03-15")  # later

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].shares == 6.0
    assert portfolio[0].entry_date == "2024-02-01"


def test_sipp_malformed_quantity_rejects_the_whole_plan_including_good_rows(
    tmp_path: Path, caplog
) -> None:  # type: ignore[type-arg]
    """Story 1.2, AC #2: under the all-or-nothing commit rule, one malformed
    row rejects the *entire* plan -- including rows that would otherwise
    have resolved to ``inserted`` (the well-formed MSFT buy here). This
    directly regression-guards against the old partial-success behavior
    where a bad row was merely skipped while everything else still
    committed."""
    import logging

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,notanumber,100.00,Buy AAPL,REF-BAD,1000.00,,5000.00\n"
        "02/02/2024,MSFT,B2,5,200.00,Buy MSFT,REF-OK,1000.00,,4000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    with caplog.at_level(logging.WARNING):
        result = agent.import_sipp(csv_path.read_bytes())

    # Nothing committed -- not even the well-formed MSFT buy.
    assert agent.get_portfolio() == []
    assert result.status == "rejected"
    assert result.skipped_rows == []
    assert len(result.failed_rows) == 1
    assert "REF-BAD" in result.failed_rows[0]
    assert "unparseable quantity" in result.failed_rows[0]
    assert any("unparseable quantity" in r.message for r in caplog.records)


def test_sipp_missing_columns_raises_and_writes_nothing(tmp_path: Path) -> None:
    # No Quantity / Running Balance columns -> reject before any DB write (#152).
    csv_text = "Date,Symbol,Price,Description\n01/02/2024,AAPL,100,Buy AAPL\n"
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    with pytest.raises(SippImportError) as exc:
        agent.import_sipp(csv_path.read_bytes())
    assert "Quantity" in str(exc.value)
    assert "Running Balance" in str(exc.value)
    assert agent.get_portfolio() == []


def test_sipp_reports_parse_errors(tmp_path: Path) -> None:
    """An unparseable Price is surfaced (not silently zeroed); the resulting
    non-positive price makes the row ``failed``, which -- under Story 1.2's
    all-or-nothing commit rule -- rejects the whole plan, including the
    Running Balance that row carried (#152, #210)."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,5,notaprice,Buy AAPL,REF-P,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())
    assert agent.get_portfolio() == []
    assert any("Price" in e for e in result.parse_errors)
    assert result.buy_count == 0
    assert result.status == "rejected"
    # Nothing was applied -- not even the Running Balance that row carried.
    assert result.cash_balance == 0.0


def test_sipp_clean_import_reports_counts(tmp_path: Path) -> None:
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,,,,Contribution,REF-C,,1000.00,1000.00\n"
        "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
        "03/01/2024,AAPL,B1,2,110.00,Sell AAPL,REF-S,,220.00,720.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes())
    assert result.buy_count == 1
    assert result.sell_count == 1
    assert result.cash_flow_count == 1
    assert result.skipped_rows == []
    assert result.parse_errors == []
    assert result.cash_balance == 720.0


def test_import_sipp_never_opens_a_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #3 (read once): the importer must parse only the bytes it was
    given, never re-open a CSV from disk for *reading*. Monkeypatch ``open``
    to blow up on any ``.csv`` path opened for reading — if ``import_sipp``
    still succeeds, it never touched the filesystem to read the CSV itself
    (#210). ``PORTFOLIO_VALUE_CSV`` is explicitly excluded from the guard:
    ``update_portfolio_snapshot``'s legacy (no-``portfolio_id``) branch may
    legitimately read/append it, and that behavior is unrelated pre-existing
    logic this story doesn't change -- excluding it by path (rather than
    relying on this test's ``cash_balance`` happening to be non-zero, which
    would otherwise skip that read incidentally) keeps the guard precise
    regardless of which branch runs.
    """
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00\n"
    )
    csv_bytes = csv_text.encode("utf-8")

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()  # unrelated CSV seeding happens here, before the guard

    real_open = open

    def _guarded_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        name = str(file)
        if name.endswith(".csv") and "r" in mode and name != str(PORTFOLIO_VALUE_CSV):
            raise AssertionError(f"import_sipp must not read a CSV path: {name}")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _guarded_open)

    result = agent.import_sipp(csv_bytes)
    assert result.buy_count == 1


def test_import_sipp_handles_crlf_terminated_csv(tmp_path: Path) -> None:
    """Regression for the io.StringIO newline nuance: unlike ``open(...,
    encoding="utf-8-sig")`` with universal-newline translation,
    ``io.StringIO`` does not translate ``\\r\\n`` -> ``\\n`` by default unless
    ``newline=None`` is passed explicitly. Built directly as bytes (not via
    ``Path.write_text``, which would apply platform-default newline
    translation on write and hide the bug) to prove a CRLF-terminated
    broker export still parses correctly (#210)."""
    csv_bytes = (
        b"Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        b"Running Balance\r\n"
        b"01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00\r\n"
    )

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_bytes)
    assert result.buy_count == 1
    assert result.parse_errors == []
    assert result.cash_balance == 5000.0

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].ticker == "AAPL"
    assert portfolio[0].shares == 10.0


def test_import_sipp_handles_bare_cr_terminated_csv(tmp_path: Path) -> None:
    """Regression for classic Mac-style line endings (bare ``\\r``, no
    ``\\n``), occasionally produced by older export tools/spreadsheets.
    ``io.StringIO`` with the default ``newline='\\n'`` raises
    ``_csv.Error: new-line character seen in unquoted field`` on this input
    -- only ``newline=None`` (universal-newline translation, matching the
    previous ``open(..., encoding="utf-8-sig")`` behavior) parses it
    correctly (#210)."""
    csv_bytes = (
        b"Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        b"Running Balance\r"
        b"01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00\r"
    )

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_bytes)
    assert result.buy_count == 1
    assert result.parse_errors == []
    assert result.cash_balance == 5000.0
