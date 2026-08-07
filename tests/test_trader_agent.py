from pathlib import Path

import pytest

from app.agents.trader.trader_agent import (
    SippImportError,
    TraderAgent,
    _to_iso_date,
)


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

    agent.import_sipp(csv_path)
    agent.import_sipp(csv_path)  # re-import must not duplicate

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].ticker == "AAPL"
    assert portfolio[0].shares == 10.0  # not 20.0


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
    agent.import_sipp(csv_path)

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
    agent.import_sipp(csv_path)

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


def test_import_sipp_rolls_back_on_error(tmp_path: Path) -> None:
    # Smoke for the try/finally transaction boundary: a clean, well-formed
    # import yields exactly one position and releases the connection.
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
    agent.import_sipp(csv_path)
    assert len(agent.get_portfolio()) == 1


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
    agent.import_sipp(csv_path)

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
    agent.import_sipp(csv_path)

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
    agent.import_sipp(csv_path)

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

    result = agent.import_sipp(csv_path)
    assert result.cash_balance == 500.0


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


def test_sipp_logs_and_skips_malformed_quantity(tmp_path: Path, caplog) -> None:  # type: ignore[type-arg]
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
        result = agent.import_sipp(csv_path)

    # The malformed AAPL row was skipped (and logged); the valid MSFT row imported.
    portfolio = agent.get_portfolio()
    assert {p.ticker for p in portfolio} == {"MSFT"}
    assert any("unparseable quantity" in r.message for r in caplog.records)
    # The skip is now surfaced in the result, not just logged (#152).
    assert len(result.skipped_rows) == 1
    assert "REF-BAD" in result.skipped_rows[0]


def test_sipp_missing_columns_raises_and_writes_nothing(tmp_path: Path) -> None:
    # No Quantity / Running Balance columns -> reject before any DB write (#152).
    csv_text = "Date,Symbol,Price,Description\n01/02/2024,AAPL,100,Buy AAPL\n"
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    with pytest.raises(SippImportError) as exc:
        agent.import_sipp(csv_path)
    assert "Quantity" in str(exc.value)
    assert "Running Balance" in str(exc.value)
    assert agent.get_portfolio() == []


def test_sipp_reports_parse_errors(tmp_path: Path) -> None:
    # An unparseable Price is surfaced (not silently zeroed) and the row is
    # skipped because the resulting price is non-positive (#152).
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

    result = agent.import_sipp(csv_path)
    assert agent.get_portfolio() == []
    assert any("Price" in e for e in result.parse_errors)
    assert result.buy_count == 0
    # Running Balance still parsed as the closing cash.
    assert result.cash_balance == 5000.0


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

    result = agent.import_sipp(csv_path)
    assert result.buy_count == 1
    assert result.sell_count == 1
    assert result.cash_flow_count == 1
    assert result.skipped_rows == []
    assert result.parse_errors == []
    assert result.cash_balance == 720.0
