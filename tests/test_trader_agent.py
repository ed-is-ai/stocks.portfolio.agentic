import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.agents.trader.trader_agent import (
    OpeningLotDuplicateError,
    SippImportError,
    TraderAgent,
    _detect_reconciliation_issues,
    _idempotency_key,
    _to_iso_date,
)
from app.core.config import PORTFOLIO_VALUE_CSV
from app.repositories import db
from app.repositories.import_receipt_repo import ImportReceiptRepository
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.trades_repo import TradesRepository
from app.services.portfolio_import.contract_schema import contract_content_digest
from app.services.portfolio_import.registry_loader import get_contract_registry


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


def test_trade_revisions_advance_only_with_committed_trade_writes(
    tmp_path: Path,
) -> None:
    """#341: all write shapes share transactional trigger invalidation."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    portfolio_id = 1
    assert agent.get_trade_revision(portfolio_id) == 0

    lot = agent.record_opening_lot(
        "TEST1", 2, 10.0, "2026-01-01", portfolio_id=portfolio_id
    )
    after_insert = agent.get_trade_revision(portfolio_id)
    agent.update_opening_lot(
        lot.id or 0, "TEST1", 3, 10.0, "2026-01-01", portfolio_id=portfolio_id
    )
    after_update = agent.get_trade_revision(portfolio_id)
    assert after_insert > 0
    assert after_update > after_insert

    sell = agent.record_sell("TEST1", 1, 11.0, "2026-01-02", portfolio_id=portfolio_id)
    before_ack = agent.get_trade_revision(portfolio_id)
    agent.set_unmatched_sell_ack(sell.id or 0, True)
    assert agent.get_trade_revision(portfolio_id) > before_ack

    assert agent.delete_opening_lot(lot.id or 0, portfolio_id) is True
    after_delete = agent.get_trade_revision(portfolio_id)
    conn = agent._conn()
    try:
        conn.execute(
            "INSERT INTO trades (ticker, action, shares, price, date, portfolio_id) "
            "VALUES ('ROLLBACK', 'BUY', 1, 1, '2026-01-03', ?)",
            (portfolio_id,),
        )
        conn.rollback()
    finally:
        conn.close()
    assert agent.get_trade_revision(portfolio_id) == after_delete


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

    agent.save_fx_rates({"2026-01-01": 10.25}, pair="GBPHKD=X")
    assert agent.get_cached_fx_rates(["2026-01-01"], pair="GBPHKD=X") == {
        "2026-01-01": 10.25
    }
    assert agent.get_cached_fx_rates(["2026-01-01"]) == {"2026-01-01": 1.35}


def test_pnl_input_revision_advances_for_changed_fx_and_currency_data(
    tmp_path: Path,
) -> None:
    """#341 review: durable P&L inputs invalidate without trade writes."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    assert agent.get_pnl_input_revision() == 0
    agent.save_fx_rates({"2026-01-01": 1.35})
    after_fx_insert = agent.get_pnl_input_revision()
    agent.save_fx_rates({"2026-01-01": 1.35})
    assert agent.get_pnl_input_revision() == after_fx_insert
    agent.save_fx_rates({"2026-01-01": 1.36})
    after_fx_update = agent.get_pnl_input_revision()
    assert after_fx_update > after_fx_insert

    agent.save_ticker_currencies({"TEST1": "USD"})
    after_currency_insert = agent.get_pnl_input_revision()
    agent.save_ticker_currencies({"TEST1": "USD"})
    assert agent.get_pnl_input_revision() == after_currency_insert
    agent.save_ticker_currencies({"TEST1": "GBP"})
    assert agent.get_pnl_input_revision() > after_currency_insert


def test_pnl_input_revision_ignores_price_refreshes_without_currency_change(
    tmp_path: Path,
) -> None:
    """#341 review: price values are not realised-P&L inputs by themselves."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    agent.save_price_cache({"TEST1": 100.0}, {"TEST1": (100.0, "GBP")})
    with_display_metadata = agent.get_pnl_input_revision()
    agent.save_price_cache({"TEST1": 101.0}, {"TEST1": (101.0, "GBP")})
    assert agent.get_pnl_input_revision() == with_display_metadata

    # GBp is a price quote unit but the same realised-P&L trading currency.
    agent.save_price_cache({"TEST1": 102.0}, {"TEST1": (102.0, "GBp")})
    assert agent.get_pnl_input_revision() == with_display_metadata

    agent.save_price_cache({"TEST1": 102.0}, {"TEST1": (102.0, "USD")})
    usd_currency = agent.get_pnl_input_revision()
    assert usd_currency > with_display_metadata

    # Removing usable display metadata makes ticker_currencies fall through
    # to the durable currency cache/live resolver, so it must invalidate.
    agent.save_price_cache({"TEST1": 102.0})
    assert agent.get_pnl_input_revision() > usd_currency


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

    agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")
    agent.import_sipp(
        csv_path.read_bytes(), account_type_id="sipp"
    )  # re-import must not duplicate

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

    first = agent.import_sipp(csv_bytes, account_type_id="sipp")
    second = agent.import_sipp(csv_bytes, account_type_id="sipp")

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

    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")

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
    agent.import_sipp(
        (header + already_imported).encode("utf-8"), account_type_id="sipp"
    )

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

    result = agent.import_sipp(mixed.encode("utf-8"), account_type_id="sipp")

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

    agent.import_sipp(first.encode("utf-8"), account_type_id="sipp")
    result = agent.import_sipp(second.encode("utf-8"), account_type_id="sipp")

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

    result = agent.import_sipp((header + rows).encode("utf-8"), account_type_id="sipp")

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
    agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

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
    agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    trades = agent.get_trade_history()
    assert len(trades) == 1
    assert trades[0].ticker == "AAPL"
    assert trades[0].date == "2024-02-01"  # not blank, import not rejected


def test_to_iso_date_strips_embedded_bom() -> None:
    # Some exports embed BOM chars mid-value (e.g. "12/10/2﻿﻿020"); the
    # date must still parse rather than being stored as a polluted string (#166).
    assert _to_iso_date("12/10/2﻿﻿﻿020") == "2020-10-12"
    assert _to_iso_date("﻿2024-02-01") == "2024-02-01"


def test_oversell_is_logged_and_visible_with_negative_shares(
    tmp_path: Path, caplog
) -> None:
    """Story 2.3: an oversell is no longer clamped to 0 -- the ticker stays
    in ``get_portfolio()``'s results with a negative ``shares`` count (the
    unconsumed shortfall, mirroring FIFO's ``UnmatchedSell`` convention),
    and the existing oversell warning still fires."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("TEST1", 10.0, 100.0, "2026-04-30")
    agent.record_sell("TEST1", 25.0, 110.0, "2026-05-01")  # oversell

    import logging

    with caplog.at_level(logging.WARNING):
        portfolio = agent.get_portfolio()

    position = next(p for p in portfolio if p.ticker == "TEST1")
    assert position.shares == -15.0
    assert any("Oversell" in r.message for r in caplog.records)


def test_replay_trades_sell_within_epsilon_of_held_is_not_an_oversell(
    caplog,
) -> None:
    """Story 2.3 review fix: the oversell check compares the *difference*
    against ``QUANTITY_EPSILON``, not a bare ``shares > s["shares"]`` --
    a SELL that is, within float precision, an exact match for the held
    quantity must never log a false "Oversell" warning or leave a
    phantom negative-dust position behind. Constructed directly against
    ``_replay_trades`` (bypassing the DB) so the epsilon-tolerance is
    exercised deterministically, independent of whether ordinary
    arithmetic happens to produce float noise naturally."""
    import logging

    rows = [
        ("TEST1", "BUY", 5.0, 100.0, "2026-01-01", None, None),
        # 2e-9 below QUANTITY_EPSILON (5e-9) -- an exact sell in every
        # practical sense, not a genuine oversell.
        ("TEST1", "SELL", 5.0 + 2e-9, 110.0, "2026-01-02", None, None),
    ]

    with caplog.at_level(logging.WARNING):
        state = TraderAgent._replay_trades(rows)

    assert not any("Oversell" in r.message for r in caplog.records)
    assert abs(state["TEST1"]["shares"]) <= 5e-9


def test_replay_trades_genuine_oversell_beyond_epsilon_still_warns() -> None:
    """The epsilon tolerance must not swallow a real oversell -- only a
    difference within ``QUANTITY_EPSILON`` is treated as a match."""
    rows = [
        ("TEST1", "BUY", 5.0, 100.0, "2026-01-01", None, None),
        ("TEST1", "SELL", 5.01, 110.0, "2026-01-02", None, None),
    ]

    state = TraderAgent._replay_trades(rows)

    assert state["TEST1"]["shares"] == -0.01


def test_refresh_portfolio_prices_keeps_oversold_ticker_visible(
    tmp_path: Path,
) -> None:
    """Story 2.3: the price-refresh path (a sibling of ``get_portfolio()``,
    with its own copy of the visibility filter) must not silently drop an
    oversold, negative-share ticker either -- it has to survive a live
    price refresh the same way it survives the initial Portfolio tab
    render."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("TEST1", 10.0, 100.0, "2026-04-30")
    agent.record_sell("TEST1", 25.0, 110.0, "2026-05-01")  # oversell -> -15

    positions = agent.refresh_portfolio_prices(current_prices={"TEST1": 120.0})

    position = next(p for p in positions if p.ticker == "TEST1")
    assert position.shares == -15.0


def test_replay_trades_buy_crossing_through_zero_does_not_crash() -> None:
    """A BUY that pushes a negative (oversold) position *through* zero to a
    new positive total -- not landing exactly on zero -- must not divide by
    a near-zero denominator and crash. Per the spec's Design Notes, making
    ``avg_cost`` economically "clean" while crossing zero is explicitly out
    of scope; this only guards against a crash, matching the exact-zero
    case's existing guard."""
    rows = [
        ("TEST1", "SELL", 3.0, 100.0, "2026-01-01", None, None),  # -> -3.0
        ("TEST1", "BUY", 3.0000001, 50.0, "2026-01-02", None, None),
    ]

    state = TraderAgent._replay_trades(rows)

    assert state["TEST1"]["shares"] == 1e-07
    assert isinstance(state["TEST1"]["avg_cost"], float)


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
    agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")
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

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

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
        agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

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
        agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

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

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

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
    agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

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
    agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].shares == 6.0
    assert portfolio[0].entry_date == "2024-01-01"


def test_to_iso_date_converts_known_formats() -> None:
    assert _to_iso_date("01/02/2024") == "2024-02-01"
    assert _to_iso_date("2024-02-01") == "2024-02-01"
    assert _to_iso_date("  2024-03-15  ") == "2024-03-15"


def test_to_iso_date_converts_2_digit_year() -> None:
    """Story 3.4: IG's export uses a 2-digit-year ``DD/MM/YY`` date, tried
    only after both 4-digit formats so a genuine 4-digit-year value is
    never misread as a 2-digit one."""
    assert _to_iso_date("01/02/24") == "2024-02-01"
    # A 4-digit-year value must still win over any 2-digit-year reading.
    assert _to_iso_date("01/02/2024") == "2024-02-01"


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
    agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

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
    """Story 1.4, AC1: EUR/USD-denominated cash-flow rows must parse with
    their real currency preserved, not silently stripped and stored as a
    bare number treated as GBP everywhere downstream (#186, #210) — a real
    SIPP export can carry a EUR interest sub-account.

    No GBP-marked row appears in this fixture, so the legacy GBP-only
    ``cash_balance``/``account_state`` field correctly stays at 0.0 (AC1's
    fix for "never present a number in a currency the source data didn't
    actually carry") -- the EUR/USD winners are separately queryable via
    ``CashBalancesRepository``. Imports into a real portfolio (not the
    legacy ``portfolio_id=None`` bucket) so the assertion isn't at the
    mercy of any other test's writes to the legacy single-portfolio state.
    """
    import sqlite3

    from app.repositories import db
    from app.repositories.cash_balances_repo import CashBalancesRepository

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
    pf = agent.create_portfolio("SIPP")

    result = agent.import_sipp(
        csv_path.read_bytes(), portfolio_id=pf.id, account_type_id="sipp"
    )

    assert result.status == "ok"
    assert result.failed_rows == []
    conn = sqlite3.connect(agent.db_path)
    rows = conn.execute(
        "SELECT flow_type, amount, currency FROM cash_flows ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [("INTEREST", 0.38, "EUR"), ("DIVIDEND", 1.25, "USD")]
    # No GBP evidence anywhere in this file -- the legacy field must not be
    # backfilled with a mislabeled non-GBP number (the exact AC1 defect).
    assert result.cash_balance == 0.0
    cash_balances = CashBalancesRepository(db.make_connect(lambda: agent.db_path))
    assert cash_balances.get(pf.id, "EUR") == (Decimal("153.61"), "2024-05-28")
    assert cash_balances.get(pf.id, "USD") == (Decimal("154.86"), "2024-05-29")
    assert cash_balances.get(pf.id, "GBP") is None


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

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.buy_count == 0 and result.sell_count == 0
    conn = sqlite3.connect(agent.db_path)
    rows = conn.execute(
        "SELECT flow_type, amount FROM cash_flows ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [("DIVIDEND", 76.50)]
    # No phantom LIGHT position was created from the Symbol column.
    assert agent.get_portfolio() == []


def test_sipp_skips_sedol_only_trade_without_creating_a_phantom_position(
    tmp_path: Path,
) -> None:
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "09/03/2021,n/a,BMC5RR2,30,16.14,30 ARCT CLEA Del,REF,484.20,,1000.00\n"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode(), account_type_id="sipp")

    assert result.buy_count == 0
    assert result.cash_flow_count == 0
    assert result.skipped_count == 1
    assert agent.get_portfolio() == []


def test_sipp_issue_detail_falls_back_to_csv_row_when_reference_is_na(
    tmp_path: Path,
) -> None:
    """When Reference is blank/"n/a" (common for interest/dividend rows),
    issue detail must still point at a specific row instead of a useless
    "row n/a" repeated for every failure (#186). Story 1.4: an unparseable
    monetary cell is now a ``failed_rows`` entry (AC3), not a
    ``parse_errors`` one -- the row-label fallback this test guards applies
    equally to that new channel."""
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

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert len(result.failed_rows) == 1
    # Row 1 is the header, so the first data row is CSV row 2.
    assert "CSV row 2" in result.failed_rows[0]
    assert "n/a" not in result.failed_rows[0].split(":")[0]


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

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")
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

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.total_rows == 2
    assert result.buy_count + result.cash_flow_count == result.total_rows
    assert result.status == "ok"


def test_sipp_unparseable_cash_amount_rejects_the_whole_plan(
    tmp_path: Path,
) -> None:
    """A non-trade row whose Debit is unparseable cannot be safely written,
    so under the all-or-nothing commit rule (Story 1.2, AC #2) the entire
    plan is rejected, not silently skipped-but-still-committed (#187,
    #210). Story 1.4, AC3: the malformed cell is now surfaced by
    ``parse_field_money`` directly, with a stable error code."""
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

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.total_rows == 1
    assert result.buy_count == 0
    assert result.sell_count == 0
    assert result.cash_flow_count == 0
    assert result.skipped_rows == []
    assert len(result.failed_rows) == 1
    assert "Debit" in result.failed_rows[0]
    assert "malformed_locale" in result.failed_rows[0]
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

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

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
        result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    # Nothing committed -- not even the well-formed MSFT buy.
    assert agent.get_portfolio() == []
    assert result.status == "rejected"
    assert result.skipped_rows == []
    assert len(result.failed_rows) == 1
    assert "REF-BAD" in result.failed_rows[0]
    assert "unparseable quantity" in result.failed_rows[0]
    assert any("unparseable quantity" in r.message for r in caplog.records)


def test_sipp_missing_columns_raises_and_writes_nothing(tmp_path: Path) -> None:
    # No Quantity / Running Balance columns -> reject before any DB write
    # (#152). Otherwise carries every other II required column (Story 3.4:
    # with IG also loaded, the "best partial match" is whichever contract
    # has fewer missing columns -- this header must stay closer to II than
    # to IG's five entirely-different required columns, to keep testing
    # II's own missing-column error text specifically).
    csv_text = (
        "Date,Symbol,Sedol,Price,Description,Reference,Debit,Credit\n"
        "01/02/2024,AAPL,B123,100,Buy AAPL,REF-1,1000,\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    with pytest.raises(SippImportError) as exc:
        agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")
    assert "Quantity" in str(exc.value)
    assert "Running Balance" in str(exc.value)
    assert agent.get_portfolio() == []


def test_sipp_reports_parse_errors(tmp_path: Path) -> None:
    """An unparseable Price is surfaced (not silently zeroed) -- Story 1.4,
    AC3 routes this directly to ``failed_rows`` (with a stable error code)
    rather than ``parse_errors``, which -- under Story 1.2's all-or-nothing
    commit rule -- rejects the whole plan, including the Running Balance
    that row carried (#152, #210)."""
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

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")
    assert agent.get_portfolio() == []
    assert any("Price" in e and "malformed_locale" in e for e in result.failed_rows)
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

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")
    assert result.buy_count == 1
    assert result.sell_count == 1
    assert result.cash_flow_count == 1
    assert result.skipped_rows == []
    assert result.parse_errors == []
    assert result.cash_balance == 720.0


def test_sipp_hkd_row_imports_successfully(tmp_path: Path) -> None:
    """Story 1.4, AC2: an HKD-denominated row must import successfully
    end-to-end (parser + row loop + DB write) -- today's ``clean_amount``
    silently fails this exact shape to 0.0 ("HK$1,234.56" strips its "$"/
    "," to "HK1234.56", which ``float()`` rejects, and the row silently
    defaults to 0.0 rather than surfacing an error)."""
    import sqlite3

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        '01/06/2024,n/a,n/a,n/a,n/a,HK dividend,n/a,n/a,"HK$1,234.56",'
        '"HK$1,234.56"\n'
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.status != "rejected"
    conn = sqlite3.connect(agent.db_path)
    rows = conn.execute(
        "SELECT flow_type, amount, currency FROM cash_flows ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [("DIVIDEND", 1234.56, "HKD")]


def test_sipp_unsupported_currency_rejects_whole_plan(tmp_path: Path) -> None:
    """Story 1.4, AC3: an unrecognized currency marker (e.g. yen) rejects
    its row -- and under Story 1.2's all-or-nothing commit rule, the whole
    plan, including an otherwise well-formed row in the same file. Nothing
    is committed, not just the one bad row."""
    import sqlite3

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Gross interest,n/a,n/a,¥123,¥123\n"
        "02/01/2024,n/a,n/a,n/a,n/a,Contribution,n/a,n/a,500.00,500.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.status == "rejected"
    assert any("unsupported_currency" in e for e in result.failed_rows)
    conn = sqlite3.connect(agent.db_path)
    trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    flow_count = conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0]
    conn.close()
    assert (trade_count, flow_count) == (0, 0)


def test_sipp_contradictory_currency_rejects_whole_plan(tmp_path: Path) -> None:
    """Story 1.4, AC3: a trade row whose Price carries a different explicit
    currency marker than its Debit is contradictory monetary evidence --
    the row (and therefore the whole plan) is rejected, not silently
    resolved to one currency or the other."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,5,€153.61,Buy AAPL,REF-FX,$1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.status == "rejected"
    assert any("contradictory" in e for e in result.failed_rows)
    assert agent.get_portfolio() == []


def test_sipp_eu_locale_and_parenthesized_negative_are_parsed_not_dropped(
    tmp_path: Path,
) -> None:
    """Story 1.4, AC4: the PRD's own ``1.234,56`` EU-format example commits
    with the correct currency and signed amount. A parenthesized-negative
    Running Balance in the same file must parse successfully (never crash
    the plan or get silently zeroed the way today's ``clean_amount`` does,
    which doesn't strip parens at all) even though -- per this story's
    documented scope boundary -- a non-positive balance still doesn't win
    the ``#158`` rank (Story 1.5 owns fixing that gate itself)."""
    import sqlite3

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        '01/01/2024,n/a,n/a,n/a,n/a,EUR contribution,n/a,n/a,"€1.234,56",'
        '"€1.234,56"\n'
        "02/01/2024,n/a,n/a,n/a,n/a,Statement note,n/a,,,(£45.00)\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.status == "ok"
    assert result.failed_rows == []
    conn = sqlite3.connect(agent.db_path)
    rows = conn.execute(
        "SELECT flow_type, amount, currency FROM cash_flows ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [("CONTRIBUTION", 1234.56, "EUR")]


def test_sipp_ambiguous_single_separator_three_digits_rejects_whole_plan(
    tmp_path: Path,
) -> None:
    """Story 1.4, AC4: a genuinely ambiguous single-separator-3-digit amount
    (could be a 3dp amount or a grouped whole number, and this story's
    supported currencies are all 2dp) is rejected rather than silently
    guessed either way."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Gross interest,n/a,n/a,€1.234,€1.234\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.status == "rejected"
    assert any("ambiguous_currency" in e for e in result.failed_rows)
    assert agent.get_portfolio() == []


def test_sipp_three_decimal_unit_price_resolved_via_row_locale_evidence(
    tmp_path: Path,
) -> None:
    """A real Ashmore SIPP row: Price "£2.351" is a single-separator/3dp
    amount that's ambiguous on its own, but Debit "£1,501.99" and Running
    Balance "£12,345.67" on the same row both mix grouping and decimal
    separators, unambiguously establishing "." as this row's decimal
    separator. That evidence resolves Price to 2.351 (not the false
    positive of a 2351x-grouped whole number) while cash fields keep
    strict ambiguity rejection."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "30/03/2022,ASHM,B132NW2,634,£2.351,634 ASHMORE GROUP  Del"
        '    2.35 S Date 30/03/22,27700FAKE01,"£1,501.99",n/a,'
        '"£12,345.67"\n'
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.status != "rejected", result.failed_rows
    conn = sqlite3.connect(agent.db_path)
    try:
        row = conn.execute(
            "SELECT ticker, action, shares, price FROM trades"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("ASHM", "BUY", 634.0, 2.351)


def test_sipp_row_with_both_debit_and_credit_rejects_whole_plan(
    tmp_path: Path,
) -> None:
    """Story 1.5, AC4: a row with both Debit and Credit populated is
    contradictory monetary evidence -- rejected as a validation error, not
    silently resolved by preferring Debit (today's ``"BUY" if debit > 0
    else "SELL" if credit > 0`` always prefers Debit when both are set)."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Contradictory row,REF-DC,100.00,50.00,500.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.status == "rejected"
    assert any(
        "REF-DC" in e and "both Debit" in e and "Credit" in e
        for e in result.failed_rows
    )
    assert agent.get_portfolio() == []


def test_sipp_row_with_only_debit_or_only_credit_imports_normally(
    tmp_path: Path,
) -> None:
    """Story 1.5, AC4 (negative case): the contradiction fix must not
    reject legitimate single-sided rows."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Withdrawal,REF-D,100.00,,400.00\n"
        "02/01/2024,n/a,n/a,n/a,n/a,Contribution,REF-C,,50.00,450.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_path.read_bytes(), account_type_id="sipp")

    assert result.status == "ok"
    assert result.cash_flow_count == 2


def test_detect_reconciliation_issues_flags_a_broken_checkpoint() -> None:
    """Story 1.5, AC5: a statement balance that doesn't reconcile against
    the intervening cash movements is detected. Rows are given in the
    SIPP export's reverse-chronological file order (newest first); the
    detector walks them in true chronological order internally."""
    rows = [
        {
            "Date": "03/01/2024",
            "Reference": "R3",
            "Debit": "",
            "Credit": "",
            "Running Balance": "600.00",
        },
        {
            "Date": "02/01/2024",
            "Reference": "R2",
            "Debit": "",
            "Credit": "100.00",
            "Running Balance": "500.00",
        },
        {
            "Date": "01/01/2024",
            "Reference": "R1",
            "Debit": "",
            "Credit": "",
            "Running Balance": "400.00",
        },
    ]
    issues = _detect_reconciliation_issues(rows)
    assert len(issues) == 1
    currency, date, prior, expected, actual, difference, row_ref = issues[0]
    assert currency == "GBP"
    assert date == "2024-01-03"
    assert prior == 500.0
    assert expected == 500.0
    assert actual == 600.0
    assert difference == 100.0
    assert row_ref == "R3"


def test_detect_reconciliation_issues_clean_fixture_reports_nothing() -> None:
    """Story 1.5, AC5 (negative case): a fully-reconciling fixture (every
    checkpoint matches prior + intervening movements) reports zero issues."""
    rows = [
        {
            "Date": "03/01/2024",
            "Reference": "R3",
            "Debit": "",
            "Credit": "",
            "Running Balance": "500.00",
        },
        {
            "Date": "02/01/2024",
            "Reference": "R2",
            "Debit": "",
            "Credit": "100.00",
            "Running Balance": "500.00",
        },
        {
            "Date": "01/01/2024",
            "Reference": "R1",
            "Debit": "",
            "Credit": "",
            "Running Balance": "400.00",
        },
    ]
    assert _detect_reconciliation_issues(rows) == []


def test_detect_reconciliation_issues_scoped_per_currency() -> None:
    """A EUR checkpoint must never reconcile against GBP/USD movements --
    each currency tracks its own independent checkpoint/movement state."""
    rows = [
        # Newest first (reverse-chronological), interleaving two currencies.
        {
            "Date": "03/01/2024",
            "Reference": "R4",
            "Debit": "",
            "Credit": "",
            "Running Balance": "€300.00",
        },
        {
            "Date": "02/01/2024",
            "Reference": "R3",
            "Debit": "",
            "Credit": "£100.00",
            "Running Balance": "£500.00",
        },
        {
            "Date": "02/01/2024",
            "Reference": "R2",
            "Debit": "",
            "Credit": "€100.00",
            "Running Balance": "€200.00",
        },
        {
            "Date": "01/01/2024",
            "Reference": "R1",
            "Debit": "",
            "Credit": "",
            "Running Balance": "£400.00",
        },
    ]
    # GBP: 400 -> +100 -> 500 (matches). EUR: first checkpoint 200 (no
    # prior EUR movement in this fixture) -> +0 -> 300 (mismatch: 200 != 300).
    issues = _detect_reconciliation_issues(rows)
    assert len(issues) == 1
    currency, date, prior, expected, actual, difference, row_ref = issues[0]
    assert currency == "EUR"
    assert row_ref == "R4"
    assert prior == 200.0
    assert expected == 200.0
    assert actual == 300.0


def test_sipp_import_persists_reconciliation_issue_end_to_end(
    tmp_path: Path,
) -> None:
    """Story 1.5, AC5: a fixture with a deliberately broken Running Balance
    produces one row in ``cash_reconciliation_issues`` after import,
    retrievable via ``list_reconciliation_issues``, and the count is
    surfaced immediately on the result."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "03/01/2024,n/a,n/a,n/a,n/a,Statement,R3,,,600.00\n"
        "02/01/2024,n/a,n/a,n/a,n/a,Contribution,R2,,100.00,500.00\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Opening,R1,,,400.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")

    result = agent.import_sipp(
        csv_path.read_bytes(), portfolio_id=pf.id, account_type_id="sipp"
    )

    assert result.status == "ok"
    assert result.reconciliation_issue_count == 1
    issues = agent.list_reconciliation_issues(pf.id)
    assert len(issues) == 1
    assert issues[0][7] == "R3"  # row_ref


def test_sipp_import_clean_fixture_records_no_reconciliation_issues(
    tmp_path: Path,
) -> None:
    """Story 1.5, AC5 (negative case): a fully-reconciling fixture produces
    zero reconciliation issues."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "03/01/2024,n/a,n/a,n/a,n/a,Statement,R3,,,500.00\n"
        "02/01/2024,n/a,n/a,n/a,n/a,Contribution,R2,,100.00,500.00\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Opening,R1,,,400.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")

    result = agent.import_sipp(
        csv_path.read_bytes(), portfolio_id=pf.id, account_type_id="sipp"
    )

    assert result.status == "ok"
    assert result.reconciliation_issue_count == 0
    assert agent.list_reconciliation_issues(pf.id) == []


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

    result = agent.import_sipp(csv_bytes, account_type_id="sipp")
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

    result = agent.import_sipp(csv_bytes, account_type_id="sipp")
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

    result = agent.import_sipp(csv_bytes, account_type_id="sipp")
    assert result.buy_count == 1
    assert result.parse_errors == []
    assert result.cash_balance == 5000.0


# --- Story 2.1: shared security identity for FIFO matching -----------------


def _sipp_row(
    date: str,
    symbol: str,
    ref: str,
    qty: str = "10",
    price: str = "100.00",
    description: str = "Buy",
    debit: str = "1000.00",
    sedol: str = "B1",
) -> str:
    return (
        f"{date},{symbol},{sedol},{qty},{price},{description},{ref},{debit},,5000.00\n"
    )


_SIPP_HEADER = (
    "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
    "Running Balance\n"
)


def test_correct_trade_regression_deletes_alias_equivalent_raw_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story 2.1: ``correct_trade()`` (a live UI action) must actually
    replace a position's trades when they're stored under a raw,
    alias-equivalent spelling different from the canonical spelling the UI
    now passes back -- the confirmed bug (silent no-op delete, double-
    counted shares) that triggered this story's scope widening to
    ``TradesRepository``."""
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    # Seed a raw, alias-equivalent trade directly -- simulates a trade
    # imported before the alias was configured.
    agent._trades.insert("ABC.L", "BUY", 10.0, 100.0, "2026-01-01")

    agent.correct_trade("ABC", 4.0, 105.0, "2026-05-01")

    history = agent.get_trade_history()
    assert len(history) == 1
    assert history[0].ticker == "ABC"
    assert history[0].shares == 4.0


def test_sipp_aliased_symbol_recorded_under_canonical_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    csv_text = _SIPP_HEADER + _sipp_row("01/02/2024", "ABC.L", "REF-A")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")

    assert result.status == "ok"
    with sqlite3.connect(agent.db_path) as conn:
        tickers = [r[0] for r in conn.execute("SELECT ticker FROM trades").fetchall()]
    assert tickers == ["ABC"]


def test_sipp_ambiguous_ticker_alias_rejects_whole_plan_including_good_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-row CSV (matching the sibling unparseable-quantity test's shape):
    one row hits a genuine cycle, one row is otherwise valid -- proving the
    whole plan is rejected, not just the bad row dropped."""
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases",
        lambda: {"ABC.L": "ABC", "ABC": "ABC.L"},
    )
    csv_text = (
        _SIPP_HEADER
        + _sipp_row("01/02/2024", "ABC.L", "REF-BAD")
        + _sipp_row("02/02/2024", "MSFT", "REF-OK", price="200.00")
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")

    assert agent.get_portfolio() == []
    assert result.status == "rejected"
    assert len(result.failed_rows) == 1
    assert "REF-BAD" in result.failed_rows[0]
    assert "ambiguous" in result.failed_rows[0].lower()
    # The good MSFT row was still provisionally classified (matching the
    # sibling unparseable-quantity test's counts) -- neither rejection
    # branch is what's being counted here, only the surviving row is.
    assert result.inserted_count == 1
    assert result.duplicate_count == 0
    assert result.total_rows == 2
    with sqlite3.connect(agent.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_sipp_symbol_resolving_to_alias_target_imports_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No alias target is reserved: any acyclic mapping imports normally."""
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases", lambda: {"XYZ": "HSFWA"}
    )
    csv_text = _SIPP_HEADER + _sipp_row("01/02/2024", "XYZ", "REF-BAD")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")

    assert {position.ticker for position in agent.get_portfolio()} == {"HSFWA"}
    assert result.status == "ok"
    assert result.failed_rows == []
    assert result.inserted_count == 1
    assert result.duplicate_count == 0


def test_sipp_uses_sedol_when_symbol_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing Symbol falls back generically to the row's SEDOL."""
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases", lambda: {"BMJJJF9": "REAL.L"}
    )
    csv_text = _SIPP_HEADER + _sipp_row(
        "01/02/2024", "n/a", "REF-A", description="Fund purchase", sedol="BMJJJF9"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")

    assert result.status == "ok"
    with sqlite3.connect(agent.db_path) as conn:
        trades = conn.execute(
            "SELECT ticker, shares, price, currency FROM trades"
        ).fetchall()
    assert trades == [("REAL.L", 10.0, 100.0, "GBP")]


def test_sipp_skips_unconfigured_sedol_only_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown SEDOL is not a quoteable holding identity."""
    monkeypatch.setattr("app.agents.trader.trader_agent.load_aliases", lambda: {})
    csv_text = _SIPP_HEADER + _sipp_row(
        "01/02/2024", "n/a", "REF-A", description="Fund purchase", sedol="BMJJJF9"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")

    assert result.status == "ok"
    assert result.skipped_count == 1
    assert agent.get_portfolio() == []


def test_replay_trades_degrades_to_raw_ticker_on_ambiguous_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Outside SIPP import, a cycle must never crash the Portfolio view --
    log a warning and fall back to the raw ticker for that trade."""
    import logging

    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases",
        lambda: {"ABC.L": "ABC", "ABC": "ABC.L"},
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._trades.insert("ABC.L", "BUY", 10.0, 100.0, "2026-01-01")

    with caplog.at_level(logging.WARNING):
        portfolio = agent.get_portfolio()

    assert {p.ticker for p in portfolio} == {"ABC.L"}
    assert any("ambiguous" in r.message.lower() for r in caplog.records)


def test_replay_trades_resolves_legacy_identity_through_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yahoo_ticker = "0P00013P6I.L"
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases",
        lambda: {"HSFWA": yahoo_ticker},
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._trades.insert("HSFWA", "BUY", 1000.0, 3.40, "2026-01-01")

    portfolio = agent.get_portfolio(current_prices={yahoo_ticker: 4.1137})

    assert len(portfolio) == 1
    assert portfolio[0].ticker == yahoo_ticker
    assert portfolio[0].current_price == 4.11
    assert portfolio[0].current_value == 4113.7


def test_replay_trades_cross_spelling_merges_into_one_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIFO/avg-cost identity agreement: two raw spellings of the same
    security fold into one running position, not two."""
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._trades.insert("ABC.L", "BUY", 10.0, 100.0, "2026-01-01")
    agent._trades.insert("ABC", "BUY", 5.0, 100.0, "2026-01-02")

    portfolio = agent.get_portfolio()

    assert len(portfolio) == 1
    assert portfolio[0].ticker == "ABC"
    assert portfolio[0].shares == 15.0


# --- GH-473: display symbol carried alongside the canonical id --------------


def test_replay_display_symbol_keeps_aliased_import_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An aliased holding keeps its raw import spelling for display while
    ``ticker`` stays the canonical id used for matching."""
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases",
        lambda: {"HSFWA": "0P00013P6I.L"},
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._trades.insert("HSFWA", "BUY", 1000.0, 3.40, "2026-01-01")

    portfolio = agent.get_portfolio()

    assert portfolio[0].ticker == "0P00013P6I.L"
    assert portfolio[0].display_symbol == "HSFWA"


def test_replay_display_symbol_equals_ticker_when_unaliased(tmp_path: Path) -> None:
    """With no alias there is no second spelling to show -- display symbol
    and canonical ticker are the same string."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._trades.insert("WCOG", "BUY", 10.0, 100.0, "2026-01-01")

    portfolio = agent.get_portfolio()

    assert portfolio[0].ticker == "WCOG"
    assert portfolio[0].display_symbol == "WCOG"


def test_replay_display_symbol_survives_chained_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-hop alias chain resolves the canonical id fully, but the
    display symbol is still the spelling actually imported."""
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases",
        lambda: {"ABC.L": "ABC", "ABC": "ABC-NEW"},
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._trades.insert("ABC.L", "BUY", 10.0, 100.0, "2026-01-01")

    portfolio = agent.get_portfolio()

    assert portfolio[0].ticker == "ABC-NEW"
    assert portfolio[0].display_symbol == "ABC.L"


@pytest.mark.parametrize("reverse", [False, True])
def test_replay_display_symbol_picks_latest_dated_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    """Two spellings folding into one position: the latest-dated one wins,
    and the choice is identical whichever order the rows are inserted in."""
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    inserts = [("ABC.L", "2026-01-02"), ("ABC", "2026-02-03")]
    for ticker, date in reversed(inserts) if reverse else inserts:
        agent._trades.insert(ticker, "BUY", 5.0, 100.0, date)

    portfolio = agent.get_portfolio()

    assert len(portfolio) == 1
    assert portfolio[0].ticker == "ABC"
    assert portfolio[0].display_symbol == "ABC"


@pytest.mark.parametrize("reverse", [False, True])
def test_replay_display_symbol_same_date_tie_breaks_lexicographically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    """Same-dated spellings have no "latest" -- the lexicographically
    smallest raw spelling breaks the tie, again order-independently."""
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.load_aliases",
        lambda: {"AAA": "CANON", "BBB": "CANON"},
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    inserts = ["BBB", "AAA"]
    for ticker in reversed(inserts) if reverse else inserts:
        agent._trades.insert(ticker, "BUY", 5.0, 100.0, "2026-02-03")

    portfolio = agent.get_portfolio()

    assert len(portfolio) == 1
    assert portfolio[0].ticker == "CANON"
    assert portfolio[0].display_symbol == "AAA"


# --- Story 2.2: deterministic replay ordering -------------------------------


def test_import_sipp_persists_source_row_index_matching_csv_position(
    tmp_path: Path,
) -> None:
    """Each inserted trade's ``source_row_index`` matches its 0-based
    position within its own source CSV file -- the only place a row's file
    position is known, so it must be captured at insert time."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-A,1000.00,,5000.00\n"
        "02/02/2024,MSFT,B456,5,200.00,Buy MSFT,REF-B,1000.00,,4000.00\n"
        "03/02/2024,AAPL,B123,3,105.00,Sell AAPL,REF-C,,315.00,4315.00\n"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")

    assert result.status == "ok"
    # Insertion order (ascending id) matches CSV row order exactly -- each
    # row's persisted source_row_index must equal its own 0-based position.
    by_insertion_order = sorted(agent._trades.history(), key=lambda t: t.id or 0)
    assert [t.source_row_index for t in by_insertion_order] == [0, 1, 2]


def test_replay_trades_skips_unparseable_date_row_log_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Average-cost replay (``_replay_trades``) skips a row with an
    unparseable date, mirroring FIFO's existing skip (Story 2.2) --
    log-only, no retrievable-data surface, no crash."""
    import logging

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._trades.insert("GOOD", "BUY", 10.0, 100.0, "2026-01-01")
    agent._trades.insert("BAD", "BUY", 5.0, 50.0, "not-a-date")

    with caplog.at_level(logging.WARNING):
        portfolio = agent.get_portfolio()

    assert {p.ticker for p in portfolio} == {"GOOD"}
    assert any(
        "unparseable" in r.message.lower() and "BAD" in r.message
        for r in caplog.records
    )


def test_replay_trades_null_source_row_index_does_not_crash(tmp_path: Path) -> None:
    """A pre-Story-2.2 row (``source_row_index IS NULL``, simulated via a
    direct insert bypassing the SIPP import) still replays without
    crashing -- the NULL sentinel is a SQL-level ``ORDER BY`` concern
    (``open_rows``), never a ``_replay_trades`` unpacking concern."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._trades.insert("TEST1", "BUY", 10.0, 100.0, "2026-01-01")

    portfolio = agent.get_portfolio()

    assert {p.ticker for p in portfolio} == {"TEST1"}


def test_same_day_one_file_avg_cost_processes_last_listed_row_first(
    tmp_path: Path,
) -> None:
    """I/O matrix: two same-ticker, same-date rows in one CSV, listed
    [most-recent, earliest] -- average-cost replay processes the earliest
    (last-listed) row first. Encoded here as a SELL listed first (most
    recent) and a BUY listed second (earliest): correct ordering replays
    the BUY before the SELL, fully closing the position (0 shares, filtered
    out of the portfolio); the old id/insertion-order bug would instead
    process the SELL first (an oversell, clamped to 0) and then the BUY,
    leaving 5 shares open."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/03/2024,ORDX,S001,5,150.00,Sell ORDX,REF-SELL,,750.00,5750.00\n"
        "01/03/2024,ORDX,S001,5,100.00,Buy ORDX,REF-BUY,500.00,,5000.00\n"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")
    assert result.status == "ok"

    portfolio = agent.get_portfolio()
    assert all(p.ticker != "ORDX" for p in portfolio)


# --- Story 2.4: trade provenance + Opening Lots -----------------------------


def test_record_buy_defaults_source_to_manual(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    trade = agent.record_buy("AAPL", 10, 100.0, "2026-01-01")
    assert trade.source == "manual"
    assert agent.get_trade_history()[0].source == "manual"


def test_record_sell_defaults_source_to_manual(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    trade = agent.record_sell("AAPL", 10, 100.0, "2026-01-01")
    assert trade.source == "manual"


def test_record_buy_accepts_explicit_source(tmp_path: Path) -> None:
    """Quick-Add (source="quick_add") and the Opening Lot path both call
    through this same parameter rather than a duplicated insert path."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    trade = agent.record_buy("AAPL", 10, 100.0, "2026-01-01", source="quick_add")
    assert trade.source == "quick_add"
    assert agent.get_trade_history()[0].source == "quick_add"


def test_correct_trade_always_tags_correction(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("AAPL", 10, 100.0, "2026-01-01", source="quick_add")
    corrected = agent.correct_trade("AAPL", 20, 110.0, "2026-01-02")
    assert corrected.source == "correction"
    history = agent.get_trade_history("AAPL")
    assert len(history) == 1
    assert history[0].source == "correction"


def test_import_sipp_stamps_source_and_one_batch_id_per_call(tmp_path: Path) -> None:
    """Every trade written by one ``import_sipp`` call shares a single
    ``import_batch_id``; a second, separate call gets a fresh one."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-A,1000.00,,5000.00\n"
        "02/02/2024,MSFT,B456,5,200.00,Buy MSFT,REF-B,1000.00,,4000.00\n"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")
    first_history = agent.get_trade_history()
    assert len(first_history) == 2
    batch_ids = {t.import_batch_id for t in first_history}
    assert len(batch_ids) == 1
    assert None not in batch_ids
    assert all(t.source == "sipp_import" for t in first_history)

    csv_text_2 = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "03/02/2024,TSLA,T789,2,300.00,Buy TSLA,REF-C,600.00,,3400.00\n"
    )
    agent.import_sipp(csv_text_2.encode("utf-8"), account_type_id="sipp")
    second_batch_trade = next(
        t for t in agent.get_trade_history() if t.ticker == "TSLA"
    )
    assert second_batch_trade.import_batch_id is not None
    assert second_batch_trade.import_batch_id not in batch_ids


def test_import_sipp_cash_flow_rows_unaffected_by_trade_provenance_columns(
    tmp_path: Path,
) -> None:
    """``source``/``import_batch_id`` (Story 2.4) are threaded only into
    ``planned_trades``/``insert_ignore`` for ``trades`` -- a cash-flow-only
    row (no Symbol/Quantity) must still import cleanly, proving the two
    write paths weren't accidentally coupled."""
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,n/a,,,,Contribution,REF-CASH,,1000.00,1000.00\n"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    result = agent.import_sipp(csv_text.encode("utf-8"), account_type_id="sipp")
    assert result.status == "ok"
    assert result.cash_flow_count == 1
    assert agent.get_trade_history() == []


def test_record_opening_lot_writes_tagged_buy(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")

    trade = agent.record_opening_lot(
        "AAPL", 10, 100.0, "2026-01-01", "opening balance", pf.id
    )

    assert trade.action == "BUY"
    assert trade.source == "opening_lot"
    history = agent.get_trade_history("AAPL", pf.id)
    assert len(history) == 1
    assert history[0].source == "opening_lot"
    # AC3: participates in average-cost replay exactly like an imported Buy.
    portfolio = agent.get_portfolio(portfolio_id=pf.id)
    assert portfolio[0].ticker == "AAPL"
    assert portfolio[0].shares == 10


def test_record_opening_lot_rejects_exact_duplicate(tmp_path: Path) -> None:
    """AC4: same canonicalized ticker/shares/date already recorded as an
    Opening Lot is rejected, not silently inserted as a second lot."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    agent.record_opening_lot("AAPL", 10, 100.0, "2026-01-01", portfolio_id=pf.id)

    with pytest.raises(OpeningLotDuplicateError):
        agent.record_opening_lot("AAPL", 10, 150.0, "2026-01-01", portfolio_id=pf.id)

    # No second row was written.
    assert len(agent.get_trade_history("AAPL", pf.id)) == 1


def test_record_opening_lot_allows_different_shares_or_date(tmp_path: Path) -> None:
    """Only an exact ticker+shares+date match is a duplicate -- a
    different quantity or date is a genuinely distinct lot."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    agent.record_opening_lot("AAPL", 10, 100.0, "2026-01-01", portfolio_id=pf.id)

    agent.record_opening_lot("AAPL", 5, 100.0, "2026-01-01", portfolio_id=pf.id)
    agent.record_opening_lot("AAPL", 10, 100.0, "2026-02-01", portfolio_id=pf.id)

    assert len(agent.get_trade_history("AAPL", pf.id)) == 3


def test_record_opening_lot_duplicate_check_scoped_per_portfolio(
    tmp_path: Path,
) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf1 = agent.create_portfolio("SIPP")
    pf2 = agent.create_portfolio("ISA")
    agent.record_opening_lot("AAPL", 10, 100.0, "2026-01-01", portfolio_id=pf1.id)

    # Same ticker/shares/date but a different portfolio is not a duplicate.
    agent.record_opening_lot("AAPL", 10, 100.0, "2026-01-01", portfolio_id=pf2.id)

    assert len(agent.get_trade_history("AAPL", pf1.id)) == 1
    assert len(agent.get_trade_history("AAPL", pf2.id)) == 1


def test_record_opening_lot_duplicate_check_resolves_ticker_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4 + Story 2.1: a duplicate is still caught when the existing
    Opening Lot is stored under a different but alias-equivalent raw
    spelling -- ``_opening_lot_duplicate_check``'s ``history()`` lookup
    resolves raw-spelling aliases the same way ``correct_trade``'s Story
    2.1 regression fix already relies on."""
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    # Seed an existing Opening Lot directly under the raw, alias-equivalent
    # spelling -- simulates one entered before the alias was configured.
    agent._trades.insert(
        "ABC.L",
        "BUY",
        10.0,
        100.0,
        "2026-01-01",
        portfolio_id=pf.id,
        source="opening_lot",
    )

    with pytest.raises(OpeningLotDuplicateError):
        agent.record_opening_lot("ABC", 10.0, 100.0, "2026-01-01", portfolio_id=pf.id)


def test_update_opening_lot_edits_fields(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    trade = agent.record_opening_lot(
        "AAPL", 10, 100.0, "2026-01-01", portfolio_id=pf.id
    )
    assert trade.id is not None

    updated = agent.update_opening_lot(
        trade.id, "AAPL", 15, 120.0, "2026-01-02", "edited", pf.id
    )

    assert updated.shares == 15
    assert updated.price == 120.0
    assert updated.date == "2026-01-02"
    history = agent.get_trade_history("AAPL", pf.id)
    assert len(history) == 1
    assert history[0].shares == 15


def test_update_opening_lot_missing_id_raises(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    with pytest.raises(ValueError):
        agent.update_opening_lot(
            99999, "AAPL", 10, 100.0, "2026-01-01", portfolio_id=pf.id
        )


def test_delete_opening_lot_removes_row(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    trade = agent.record_opening_lot(
        "AAPL", 10, 100.0, "2026-01-01", portfolio_id=pf.id
    )
    assert trade.id is not None

    deleted = agent.delete_opening_lot(trade.id, pf.id)

    assert deleted is True
    assert agent.get_trade_history("AAPL", pf.id) == []


def test_delete_opening_lot_never_deletes_non_opening_lot_trade(tmp_path: Path) -> None:
    """Defense in depth: the Opening-Lot-specific delete path refuses to
    remove a trade that isn't tagged ``source="opening_lot"``, even given
    its exact id."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    trade = agent.record_buy("AAPL", 10, 100.0, "2026-01-01", portfolio_id=pf.id)
    assert trade.id is not None

    deleted = agent.delete_opening_lot(trade.id, pf.id)

    assert deleted is False
    assert len(agent.get_trade_history("AAPL", pf.id)) == 1


# --- GH-311: import receipt / row-lineage provenance -----------------------


def _provenance_counts(db_path: Path) -> dict[str, int]:
    """Row counts across the two new GH-311 provenance tables -- a sibling
    to ``_table_counts`` (kept separate so none of the existing exact-dict
    equality assertions in ``_table_counts``'s callers need to change)."""
    conn = sqlite3.connect(db_path)
    try:
        receipts = conn.execute(
            "SELECT COUNT(*) FROM portfolio_import_receipts"
        ).fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM portfolio_import_rows").fetchone()[0]
    finally:
        conn.close()
    return {"portfolio_import_receipts": receipts, "portfolio_import_rows": rows}


_II_CSV_HEADER = (
    "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
    "Running Balance\n"
)


def test_import_sipp_rejected_plan_writes_zero_provenance_rows(
    tmp_path: Path,
) -> None:
    """GH-311: a rejected plan (a failed row present) writes zero receipt
    and zero row-lineage rows, exactly like it writes zero trades/cash
    flows today."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    csv_text = _II_CSV_HEADER + (
        "01/01/2024,n/a,,,,Contribution,REF-C,,1000.00,1000.00\n"
        "03/01/2024,MSFT,B2,notanumber,100.00,Buy MSFT,REF-BAD,1000.00,,1500.00\n"
    )

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

    assert result.status == "rejected"
    assert result.receipt_id is None
    assert _provenance_counts(agent.db_path) == {
        "portfolio_import_receipts": 0,
        "portfolio_import_rows": 0,
    }


def test_import_sipp_rollback_when_receipt_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH-311: injecting a failure inside the new receipt write itself
    rolls back the *entire* transaction -- proving the new writes share
    the pre-existing trades/cash_flows/account_state/snapshot
    transaction, not a separate one."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    def _boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("boom: receipt write failed")

    monkeypatch.setattr(ImportReceiptRepository, "insert_receipt_on_connection", _boom)

    csv_text = _II_CSV_HEADER + (
        "01/01/2024,n/a,,,,Contribution,REF-C,,1000.00,1000.00\n"
        "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    with pytest.raises(RuntimeError, match="boom"):
        agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

    counts = _table_counts(agent.db_path, pf.id)
    assert counts == {
        "trades": 0,
        "cash_flows": 0,
        "account_state": 0,
        "portfolio_snapshots": 0,
    }
    assert _provenance_counts(agent.db_path) == {
        "portfolio_import_receipts": 0,
        "portfolio_import_rows": 0,
    }


def test_import_sipp_receipt_records_contract_identity_and_counts(
    tmp_path: Path,
) -> None:
    """GH-311, AC1: a successful commit writes one receipt row with the
    correct contract id/version/content digest, source digest, and the
    four outcome counts matching the returned ``SippImportResult`` -- plus
    one row-lineage entry per source data row."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    csv_bytes = (
        _II_CSV_HEADER
        + "01/01/2024,n/a,,,,Contribution,REF-C,,1000.00,1000.00\n"
        + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    ).encode("utf-8")

    contract = get_contract_registry().detect(_II_CSV_HEADER.strip("\n").split(","))
    expected_source_digest = hashlib.sha256(csv_bytes).hexdigest()
    expected_contract_digest = contract_content_digest(contract)

    result = agent.import_sipp(csv_bytes, pf.id, account_type_id="sipp")

    assert result.status == "ok"
    assert result.receipt_id is not None

    conn = sqlite3.connect(agent.db_path)
    try:
        receipt = conn.execute(
            "SELECT contract_id, contract_version, contract_content_digest, "
            "source_digest, status, inserted_count, duplicate_count, "
            "skipped_count, failed_count FROM portfolio_import_receipts "
            "WHERE id = ?",
            (result.receipt_id,),
        ).fetchone()
        row_count = conn.execute(
            "SELECT COUNT(*) FROM portfolio_import_rows WHERE receipt_id = ?",
            (result.receipt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert receipt == (
        contract.contract_id,
        contract.version,
        expected_contract_digest,
        expected_source_digest,
        result.status,
        result.inserted_count,
        result.duplicate_count,
        result.skipped_count,
        len(result.failed_rows),
    )
    assert row_count == 2


def test_import_sipp_duplicate_row_lineage_has_no_link(
    tmp_path: Path,
) -> None:
    """GH-311: importing the same CSV twice records the second import's
    row-lineage entry for the repeated row as outcome ``duplicate`` with
    no trade/cash-flow link -- nothing new was created for it."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    csv_text = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    first = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")
    assert first.status == "ok"
    second = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")
    assert second.status == "ok"
    assert second.duplicate_count == 1
    assert second.receipt_id is not None

    conn = sqlite3.connect(agent.db_path)
    try:
        lineage = conn.execute(
            "SELECT outcome, trade_id, cash_flow_id FROM portfolio_import_rows "
            "WHERE receipt_id = ?",
            (second.receipt_id,),
        ).fetchall()
    finally:
        conn.close()

    assert lineage == [("duplicate", None, None)]


def test_import_sipp_calls_contract_detect_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH-311, AC5: the receipt's contract identity/digest must come from
    the single ``detect()`` call ``import_sipp`` already makes -- never a
    second commit-time lookup."""
    from collections.abc import Sequence

    from app.services.portfolio_import.contract_registry import ContractRegistry
    from app.services.portfolio_import.contract_schema import (
        PortfolioImportContractV1,
    )

    calls: list[Sequence[str]] = []
    original_detect = ContractRegistry.detect

    def _spy(
        self: ContractRegistry, header_row: Sequence[str]
    ) -> PortfolioImportContractV1:
        calls.append(header_row)
        return original_detect(self, header_row)

    monkeypatch.setattr(ContractRegistry, "detect", _spy)

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    csv_text = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )
    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

    assert result.status == "ok"
    assert len(calls) == 1


def test_init_trades_db_migration_is_idempotent_for_provenance_tables(
    tmp_path: Path,
) -> None:
    """GH-311: the new tables are additive ``CREATE TABLE IF NOT EXISTS``
    -- running the migration twice on a database with historical,
    pre-provenance trades is a no-op the second time, those legacy rows
    stay fully readable, and no receipt is fabricated for them."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")
    agent.record_buy("AAPL", 10, 100.0, "2020-01-01", portfolio_id=pf.id)

    conn = sqlite3.connect(agent.db_path)
    try:
        db.init_trades_db(conn)
        db.init_trades_db(conn)
    finally:
        conn.close()

    assert len(agent.get_trade_history("AAPL", pf.id)) == 1
    assert _provenance_counts(agent.db_path) == {
        "portfolio_import_receipts": 0,
        "portfolio_import_rows": 0,
    }


def test_deleting_a_sipp_imported_trade_does_not_violate_provenance_fk(
    tmp_path: Path,
) -> None:
    """GH-311 review finding: ``portfolio_import_rows.trade_id`` referencing
    ``trades(id)`` with ``PRAGMA foreign_keys = ON`` and no ``ON DELETE``
    clause would make deleting any SIPP-imported trade raise
    ``sqlite3.IntegrityError`` -- verified live before this fix. The FK
    must be ``ON DELETE SET NULL``: the delete succeeds and the
    row-lineage audit entry survives (outcome/digest intact) with its
    ``trade_id`` link cleared, since the trade it pointed to no longer
    exists."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    csv_text = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )
    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")
    assert result.status == "ok"
    trade_id = agent.get_trade_history("AAPL", pf.id)[0].id
    assert trade_id is not None

    assert agent.delete_trade(trade_id) is True

    conn = sqlite3.connect(agent.db_path)
    try:
        lineage = conn.execute(
            "SELECT outcome, trade_id FROM portfolio_import_rows WHERE receipt_id = ?",
            (result.receipt_id,),
        ).fetchall()
    finally:
        conn.close()
    assert lineage == [("inserted", None)]


def test_deleting_a_portfolio_with_sipp_imports_does_not_violate_provenance_fk(
    tmp_path: Path,
) -> None:
    """GH-311 review finding: ``delete_portfolio`` bulk-deletes trades and
    cash_flows directly (``PortfoliosRepository.delete``) -- a distinct
    code path from ``delete_trade`` that hit the identical FK violation
    before the ``ON DELETE SET NULL`` fix."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test SIPP")

    csv_text = _II_CSV_HEADER + (
        "01/01/2024,n/a,,,,Contribution,REF-C,,1000.00,1000.00\n"
        "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )
    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")
    assert result.status == "ok"

    assert agent.delete_portfolio(pf.id) is True


# --- Story 3.3: provider/account-type validation + preview_rows ------------


def _make_agent(tmp_path: Path) -> TraderAgent:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    return agent


def test_import_sipp_raises_on_provider_mismatch(tmp_path: Path) -> None:
    """A ``provider_id`` that doesn't match the CSV's auto-detected
    contract raises ``SippImportError`` -- tampering/mismatch (I/O matrix)."""
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")
    csv_text = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    with pytest.raises(SippImportError):
        agent.import_sipp(
            csv_text.encode("utf-8"),
            pf.id,
            provider_id="fake_broker",
            account_type_id="sipp",
        )


def test_import_sipp_raises_when_account_type_missing_for_contract_with_types(
    tmp_path: Path,
) -> None:
    """A contract that declares account types requires one -- omitted or
    blank must raise ``SippImportError`` naming the valid options, not
    silently import with no account type recorded (Spec Change Log fix)."""
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")
    csv_text = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    with pytest.raises(SippImportError, match="sipp"):
        agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id=None)

    with pytest.raises(SippImportError, match="sipp"):
        agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="   ")


def test_import_sipp_raises_when_account_type_not_a_declared_value(
    tmp_path: Path,
) -> None:
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")
    csv_text = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    with pytest.raises(SippImportError, match="sipp"):
        agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="isa")


def test_import_sipp_raises_when_account_type_given_for_contract_with_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symmetric with the missing-account-type case: a contract that
    declares NO account types must reject any non-blank ``account_type_id``
    too -- there is nothing to validate it against, so nothing can be
    trusted or echoed back (Spec Change Log fix)."""
    import json

    from app.services.portfolio_import.contract_registry import ContractRegistry

    contracts_dir = tmp_path / "no_account_type_contracts"
    contracts_dir.mkdir()
    (contracts_dir / "no_type.json").write_text(
        json.dumps(
            {
                "contract_id": "no_type",
                "version": "1",
                "provider_id": "no_type_provider",
                "provider_name": "No Type Provider",
                "encoding": "utf-8-sig",
                "delimiter": ",",
                "required_columns": ["Date", "Amount"],
                "field_mapping": {"Date": "date", "Amount": "debit"},
            }
        ),
        encoding="utf-8",
    )
    no_account_type_registry = ContractRegistry.load(contracts_dir)
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.get_contract_registry",
        lambda: no_account_type_registry,
    )

    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")
    csv_text = "Date,Amount\n01/01/2024,100\n"

    with pytest.raises(SippImportError):
        agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="anything")


def test_preview_rows_present_only_on_rejected_status(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")
    ok_csv = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )
    ok_result = agent.import_sipp(ok_csv.encode("utf-8"), pf.id, account_type_id="sipp")
    assert ok_result.status == "ok"
    assert ok_result.preview_rows == []

    bad_csv = _II_CSV_HEADER + (
        "03/01/2024,MSFT,B2,notanumber,100.00,Buy MSFT,REF-BAD,1000.00,,1500.00\n"
    )
    rejected_result = agent.import_sipp(
        bad_csv.encode("utf-8"), pf.id, account_type_id="sipp"
    )
    assert rejected_result.status == "rejected"
    assert rejected_result.preview_rows != []


def test_preview_rows_capped_at_twenty_and_shares_identical_keys(
    tmp_path: Path,
) -> None:
    """Preview is capped at 20 rows, and every row shares exactly the same
    key set (derived from ``CanonicalField``'s enum order) even when the
    source rows differ in which fields actually parsed -- prevents the
    preview table from misaligning values under the wrong headers."""
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")

    # 25 good trade rows plus one bad row (to force rejection), plus a
    # benign cash-only row with a different subset of populated fields.
    rows = [
        f"0{(i % 9) + 1}/01/2024,AAPL,B1,1,100.00,Buy AAPL,REF-{i},10.00,,{1000 + i}.00\n"
        for i in range(25)
    ]
    rows.append("26/01/2024,n/a,,,,Contribution,REF-CASH,,50.00,1050.00\n")
    rows.append(
        "27/01/2024,MSFT,B2,notanumber,100.00,Buy MSFT,REF-BAD,1000.00,,1500.00\n"
    )
    csv_text = _II_CSV_HEADER + "".join(rows)

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

    assert result.status == "rejected"
    assert len(result.preview_rows) == 20
    keys = {frozenset(row.keys()) for row in result.preview_rows}
    assert len(keys) == 1


def test_preview_rows_truncates_free_text_fields_harder(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")

    long_description = "D" * 100
    long_reference = "R" * 100
    csv_text = _II_CSV_HEADER + (
        f"01/01/2024,AAPL,B1,5,100.00,{long_description},{long_reference},"
        "1000.00,,5000.00\n"
        "02/01/2024,MSFT,B2,notanumber,100.00,Buy MSFT,REF-BAD,1000.00,,1500.00\n"
    )

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

    assert result.status == "rejected"
    row = result.preview_rows[0]
    assert len(row["description"]) == 41  # 40 chars + "…"
    assert row["description"].endswith("…")
    assert len(row["reference"]) == 41
    assert row["reference"].endswith("…")


def test_successful_import_echoes_provider_account_type_and_version(
    tmp_path: Path,
) -> None:
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")
    csv_text = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")

    assert result.status == "ok"
    assert result.provider_id == "interactive_investor"
    assert result.provider_name == "Interactive Investor"
    assert result.contract_version == "1"
    assert result.account_type_id == "sipp"


def test_receipt_records_callers_validated_account_type_not_contracts_full_set(
    tmp_path: Path,
) -> None:
    """Deferred-work fix (Story 3.2 review): the receipt must record the
    caller's actual per-request selection, never the contract's full
    supported set."""
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Test SIPP")
    csv_text = (
        _II_CSV_HEADER + "02/01/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B,500.00,,500.00\n"
    )

    result = agent.import_sipp(csv_text.encode("utf-8"), pf.id, account_type_id="sipp")
    assert result.status == "ok"

    conn = sqlite3.connect(agent.db_path)
    try:
        account_type_id = conn.execute(
            "SELECT account_type_id FROM portfolio_import_receipts WHERE id = ?",
            (result.receipt_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert account_type_id == "sipp"


# --- Story 3.4: IG, a contract-only provider (text extraction, signed-amount
# splitting, and the additive cash-balance fallback) --------------------------

_IG_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "ig"


def _ig_fixture(name: str) -> bytes:
    return (_IG_FIXTURES_DIR / name).read_bytes()


def test_import_sipp_ig_valid_mixed_file_produces_correct_counts(
    tmp_path: Path,
) -> None:
    """A fabricated, IG-shaped CSV (trades, commission, a fee row, cash
    in/out, an inter-account transfer) imports through the exact same
    generic ``import_sipp`` path as II -- no IG-specific business-logic
    branch is involved (AC1).

    Imported into an explicit portfolio (never the legacy no-``portfolio_
    id`` path) so this test's cash balance is isolated in this test's own
    ``portfolio_snapshots``/``account_state`` rows, never the real,
    process-shared ``data/portfolio_value.csv`` the legacy path reads/
    writes -- every new Story 3.4 test below does the same, for the same
    reason.
    """
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("IG Test")

    result = agent.import_sipp(
        _ig_fixture("valid_mixed.csv"),
        pf.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )

    assert result.status == "ok"
    assert result.provider_id == "ig"
    assert result.provider_name == "IG"
    assert result.contract_version == "1"
    assert result.buy_count == 1
    assert result.sell_count == 1
    # Commission, Section 31 fee, Cash In, Cash Out, inter-account transfer.
    assert result.cash_flow_count == 5
    assert result.failed_rows == []
    history = agent.get_trade_history(portfolio_id=pf.id)
    assert {t.ticker for t in history} == {"NIMBUS ROBOTICS", "VANTAGE CEREAL"}
    # Net delta: credits (960 + 500 + 300) - debits (910 + 8.50 + 2 + 150).
    assert result.cash_balance == pytest.approx(689.50)
    assert agent.get_cash_balance(pf.id) == pytest.approx(689.50)


def test_import_sipp_ig_malformed_cons_text_rejects_naming_the_text(
    tmp_path: Path,
) -> None:
    """``required_marker`` (review pass 2's corrected behavior): a
    ``MarketName`` that contains "CONS" but doesn't match the full
    extraction pattern (missing ``@price``) rejects the whole plan,
    naming the unparseable text -- never silently reclassified as a cash
    flow. The row's ``PL Amount`` is otherwise perfectly valid, so the
    rejection is unambiguously attributable to the ``required_marker``
    path, not a coincidental amount-parse failure."""
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("IG Test")

    result = agent.import_sipp(
        _ig_fixture("malformed_cons.csv"),
        pf.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )

    assert result.status == "rejected"
    assert len(result.failed_rows) == 1
    assert "Fictional Widgets CONS ten shares" in result.failed_rows[0]
    assert agent.get_trade_history(portfolio_id=pf.id) == []
    assert agent.get_cash_balance(pf.id) is None


def test_import_sipp_ig_cfd_row_rejects_whole_plan(tmp_path: Path) -> None:
    """A ``Period`` value other than the share-dealing placeholder ``"-"``
    (CFD/spread-bet activity) rejects the whole plan -- this app's
    canonical portfolio domain does not model CFDs."""
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("IG Test")

    result = agent.import_sipp(
        _ig_fixture("cfd_row.csv"),
        pf.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )

    assert result.status == "rejected"
    assert len(result.failed_rows) == 1
    assert "Period" in result.failed_rows[0]
    assert agent.get_trade_history(portfolio_id=pf.id) == []


def test_import_sipp_ig_duplicate_overlap_is_idempotent(tmp_path: Path) -> None:
    """Re-importing an overlapping IG file reports every repeated row as
    ``duplicate`` and never double-counts the cash-balance delta -- the
    additive-fallback analogue of II's Running Balance dedup guarantee."""
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("IG Test")

    first = agent.import_sipp(
        _ig_fixture("valid_mixed.csv"),
        pf.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )
    assert first.status == "ok"
    balance_after_first = agent.get_cash_balance(pf.id)
    assert balance_after_first is not None

    second = agent.import_sipp(
        _ig_fixture("duplicate_overlap.csv"),
        pf.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )

    assert second.status == "ok"
    assert second.duplicate_count == 7
    assert second.inserted_count == 1
    # Only the one new row (Orbital Foods, -180.00 debit) is added on top.
    assert agent.get_cash_balance(pf.id) == pytest.approx(balance_after_first - 180.0)

    # Re-importing the exact same file again must change nothing further.
    third = agent.import_sipp(
        _ig_fixture("valid_mixed.csv"),
        pf.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )
    assert third.status == "ok"
    assert third.inserted_count == 0
    assert agent.get_cash_balance(pf.id) == pytest.approx(balance_after_first - 180.0)


def test_import_sipp_ig_balance_is_order_independent(tmp_path: Path) -> None:
    """Two non-overlapping IG files import to the same final balance
    regardless of order -- the additive delta-sum is commutative."""
    ab_dir = tmp_path / "ab"
    ab_dir.mkdir()
    ba_dir = tmp_path / "ba"
    ba_dir.mkdir()

    agent_ab = _make_agent(ab_dir)
    pf_ab = agent_ab.create_portfolio("AB")
    agent_ab.import_sipp(
        _ig_fixture("balance_order_a.csv"),
        pf_ab.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )
    agent_ab.import_sipp(
        _ig_fixture("balance_order_b.csv"),
        pf_ab.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )

    agent_ba = _make_agent(ba_dir)
    pf_ba = agent_ba.create_portfolio("BA")
    agent_ba.import_sipp(
        _ig_fixture("balance_order_b.csv"),
        pf_ba.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )
    agent_ba.import_sipp(
        _ig_fixture("balance_order_a.csv"),
        pf_ba.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )

    assert agent_ab.get_cash_balance(pf_ab.id) == agent_ba.get_cash_balance(pf_ba.id)


def test_import_sipp_ig_2_digit_year_date_persists_correctly(tmp_path: Path) -> None:
    """The IG fixtures' ``DD/MM/YY`` dates (e.g. ``01/03/24``) resolve to
    the correct ISO date on the persisted trade."""
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("IG Test")

    agent.import_sipp(
        _ig_fixture("valid_mixed.csv"),
        pf.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )

    history = {t.ticker: t for t in agent.get_trade_history(portfolio_id=pf.id)}
    assert history["NIMBUS ROBOTICS"].date == "2024-03-01"


def test_import_sipp_mixed_provider_ig_delta_applied_after_dated_ii_import(
    tmp_path: Path,
) -> None:
    """The regression test for review pass 1's bug: once a dated (running-
    balance) II import has ever set a stored as-of date for a portfolio,
    a subsequent undated IG delta must still be applied -- never silently
    frozen at the II snapshot forever.

    Before the fix, routing the IG delta through ``_apply_import_cash_
    balance``'s ``#160`` stale-date guard (with ``as_of=None``) meant the
    guard's ``stored_date is None or as_of >= stored_date`` condition was
    permanently false once ``stored_date`` was set by the II import, so
    every subsequent IG delta was silently dropped with no error.
    """
    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("Mixed Provider")

    ii_result = agent.import_sipp(
        _ig_fixture("dated_ii_small.csv"),
        pf.id,
        provider_id="interactive_investor",
        account_type_id="sipp",
    )
    assert ii_result.status == "ok"
    balance_after_ii = agent.get_cash_balance(pf.id)
    assert balance_after_ii == 9000.0
    assert balance_after_ii is not None

    ig_result = agent.import_sipp(
        _ig_fixture("mixed_provider_ig.csv"),
        pf.id,
        provider_id="ig",
        account_type_id="share_dealing",
    )

    assert ig_result.status == "ok"
    balance_after_ig = agent.get_cash_balance(pf.id)
    # The bug this story fixes: before the fix, balance_after_ig would
    # equal balance_after_ii unchanged (the delta silently dropped). The
    # fixture's one IG row is a -100.00 debit (a CONS buy).
    assert balance_after_ig == pytest.approx(balance_after_ii - 100.0)
    assert balance_after_ig != balance_after_ii


def test_import_sipp_ig_non_gbp_fallback_delta_rejects_whole_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cash-balance fallback writes to the legacy GBP-only single-
    value slot -- a contributing row resolving to a non-GBP currency must
    reject the whole plan (naming the row and its currency), never be
    silently excluded from the sum (money must not silently vanish from a
    reported balance).

    Constructed via a small synthetic contract (registered alongside the
    real shipped contracts for the duration of this test) rather than the
    real IG contract, since ``signed_amount_column`` always emits a plain
    numeric string with no currency marker by design -- this scenario
    only arises for a hypothetical contract using plain ``field_mapping``
    debit/credit columns (like II) with no ``running_balance`` mapping.
    """
    from app.services.portfolio_import.contract_registry import ContractRegistry

    contract_dir = tmp_path / "contracts"
    contract_dir.mkdir()
    contract_json = {
        "contract_id": "test_no_running_balance",
        "version": "1",
        "provider_id": "test_no_running_balance",
        "provider_name": "Test No Running Balance",
        "encoding": "utf-8-sig",
        "delimiter": ",",
        "required_columns": ["Date", "Description", "Reference", "Debit", "Credit"],
        "field_mapping": {
            "Date": "date",
            "Description": "description",
            "Reference": "reference",
            "Debit": "debit",
            "Credit": "credit",
        },
    }
    (contract_dir / "test_no_running_balance_1.json").write_text(
        json.dumps(contract_json), encoding="utf-8"
    )
    custom_registry = ContractRegistry.load(contract_dir)

    agent = _make_agent(tmp_path)
    pf = agent.create_portfolio("IG Test")
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.get_contract_registry",
        lambda: custom_registry,
    )
    csv_text = (
        "Date,Description,Reference,Debit,Credit\n"
        "01/02/2024,Fictional dividend,REF-USD-1,,$100.00\n"
    )
    result = agent.import_sipp(
        csv_text.encode("utf-8"),
        pf.id,
        provider_id="test_no_running_balance",
    )

    assert result.status == "rejected"
    assert len(result.failed_rows) == 1
    assert "USD" in result.failed_rows[0]
    assert agent.get_cash_balance(pf.id) is None


def _replay_state(shares: float, avg_cost: float) -> dict:
    """Minimal replay-state dict accepted by ``_build_position``."""
    return {
        "shares": shares,
        "avg_cost": avg_cost,
        "entry_date": "2024-01-02",
        "entry_price": avg_cost,
        "stop_loss": None,
    }


def test_build_position_pence_quote_valued_in_pounds() -> None:
    """An LSE ``"GBp"`` quote is valued from the ÷100 ``current_prices``
    entry, not from the raw pence figure in ``display_info`` (#383)."""
    state = _replay_state(shares=1717.0, avg_cost=13.97)
    pos = TraderAgent._build_position(
        "WCOG",
        state,
        {"WCOG": 14.16},
        display_info={"WCOG": (1416.0, "GBp")},
    )
    assert pos.current_price == 14.16
    assert pos.current_value == round(14.16 * 1717.0, 2)
    assert pos.price_currency == "GBP"


def test_build_position_gbx_quote_valued_in_pounds() -> None:
    """``"GBX"`` is the same LSE pence unit as ``"GBp"`` (#383)."""
    state = _replay_state(shares=1717.0, avg_cost=13.97)
    pos = TraderAgent._build_position(
        "WCOG",
        state,
        {"WCOG": 14.16},
        display_info={"WCOG": (1416.0, "GBX")},
    )
    assert pos.current_price == 14.16
    assert pos.current_value == round(14.16 * 1717.0, 2)
    assert pos.price_currency == "GBP"


def test_build_position_pence_quote_missing_price_degrades_to_unpriced() -> None:
    """A pence holding absent from ``current_prices`` degrades to unpriced
    rather than reporting a wrong-scale pence figure (#383)."""
    state = _replay_state(shares=1717.0, avg_cost=13.97)
    pos = TraderAgent._build_position(
        "WCOG",
        state,
        {},
        display_info={"WCOG": (1416.0, "GBp")},
    )
    assert pos.current_price is None
    assert pos.current_value is None
    assert pos.unrealised_pnl is None


def test_build_position_usd_quote_still_native() -> None:
    """USD holdings keep the native-quote valuation path (#383)."""
    state = _replay_state(shares=100.0, avg_cost=40.0)
    pos = TraderAgent._build_position(
        "AAPL",
        state,
        {"AAPL": 44.0},
        display_info={"AAPL": (59.78, "USD")},
    )
    assert pos.current_price == 59.78
    assert pos.price_currency == "USD"


def test_build_position_gbp_lse_reads_current_prices_not_display_info() -> None:
    """An already-``"GBP"`` LSE holding is valued from ``current_prices``,
    not ``display_info[0]`` (distinct numbers pin the source) (#383)."""
    state = _replay_state(shares=100.0, avg_cost=7.0)
    pos = TraderAgent._build_position(
        "BP",
        state,
        {"BP": 7.30},
        display_info={"BP": (7.18, "GBP")},
    )
    assert pos.current_price == 7.30
    assert pos.price_currency == "GBP"


def test_earliest_snapshot_timestamp_returns_the_oldest_row(tmp_path: Path) -> None:
    """Backs the chart-range availability check (#498): the oldest stored
    snapshot, regardless of insertion order."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test")

    agent._snapshots.append(pf.id, "2026-08-20T00:00:00+00:00", 100.0, 90.0, 10.0)
    agent._snapshots.append(pf.id, "2026-08-10T00:00:00+00:00", 100.0, 90.0, 10.0)
    agent._snapshots.append(pf.id, "2026-08-30T00:00:00+00:00", 100.0, 90.0, 10.0)

    assert agent.earliest_snapshot_timestamp(pf.id) == "2026-08-10T00:00:00+00:00"


def test_earliest_snapshot_timestamp_none_when_no_snapshots(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("Test")

    assert agent.earliest_snapshot_timestamp(pf.id) is None
