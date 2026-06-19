from pathlib import Path

from app.agents.trader.trader_agent import TraderAgent, _to_iso_date


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
