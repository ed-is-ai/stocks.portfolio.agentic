from pathlib import Path

from agents.trader.trader_agent import TraderAgent


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
