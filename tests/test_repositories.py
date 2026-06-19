"""Unit tests for the repository layer (temp-file SQLite + artifact files)."""

import pytest

from app.repositories import db
from app.repositories.account_repo import AccountStateRepository
from app.repositories.alerts_repo import AlertsRepository
from app.repositories.artifacts_repo import ArtifactsRepository
from app.repositories.cash_flows_repo import CashFlowsRepository
from app.repositories.price_cache_repo import PriceCacheRepository
from app.repositories.results_repo import ResultsRepository
from app.repositories.trades_repo import TradesRepository


@pytest.fixture
def trades_connect(tmp_path):
    """A Connect factory over an initialised temp trades.db."""
    path = tmp_path / "trades.db"
    connect = db.make_connect(lambda: path)
    with db.session(connect) as conn:
        db.init_trades_db(conn)
    return connect


# --- TradesRepository ------------------------------------------------------


def test_trades_insert_and_history(trades_connect):
    repo = TradesRepository(trades_connect)
    tid = repo.insert("AAPL", "BUY", 10, 100.0, "01/02/2024", "note")
    assert isinstance(tid, int) and tid > 0
    history = repo.history()
    assert len(history) == 1
    assert history[0].ticker == "AAPL"
    assert history[0].action == "BUY"


def test_trades_history_filter_and_delete(trades_connect):
    repo = TradesRepository(trades_connect)
    repo.insert("AAPL", "BUY", 10, 100.0, "01/02/2024")
    msft_id = repo.insert("MSFT", "BUY", 5, 200.0, "02/02/2024")
    assert len(repo.history("AAPL")) == 1
    assert repo.delete_by_id(msft_id) is True
    assert repo.delete_by_id(msft_id) is False
    repo.delete_by_ticker("AAPL")
    assert repo.history() == []


def test_trades_open_rows_excludes_invalid(trades_connect):
    repo = TradesRepository(trades_connect)
    repo.insert("AAPL", "BUY", 10, 100.0, "01/02/2024")
    repo.insert("n/a", "BUY", 1, 1.0, "01/02/2024")
    rows = repo.open_rows()
    assert [r[0] for r in rows] == ["AAPL"]


def test_trades_insert_ignore_dedupes_reference(trades_connect):
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(conn, "AAPL", "BUY", 10, 100.0, "01/02/2024", "", "REF1")
        repo.insert_ignore(conn, "AAPL", "BUY", 10, 100.0, "01/02/2024", "", "REF1")
    assert len(repo.history("AAPL")) == 1


# --- CashFlowsRepository ---------------------------------------------------


def test_cash_flows_insert_ignore_dedupes(trades_connect):
    repo = CashFlowsRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(conn, "01/02/2024", "DIVIDEND", None, 12.5, "Div", "R1")
        repo.insert_ignore(conn, "01/02/2024", "DIVIDEND", None, 12.5, "Div", "R1")
    with db.session(trades_connect) as conn:
        count = conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0]
    assert count == 1


# --- PriceCacheRepository --------------------------------------------------


def test_price_cache_upsert_and_load(trades_connect):
    repo = PriceCacheRepository(trades_connect)
    repo.upsert_many({"AAPL": 100.0}, {"AAPL": (130.0, "USD")})
    repo.upsert_many({"AAPL": 110.0})  # update existing
    rows = repo.load_all()
    assert len(rows) == 1
    assert rows[0][0] == "AAPL"
    assert rows[0][1] == 110.0


# --- AccountStateRepository ------------------------------------------------


def test_account_state_set_get_exists(trades_connect):
    repo = AccountStateRepository(trades_connect)
    assert repo.exists("cash_balance") is False
    assert repo.get("cash_balance") is None
    repo.set("cash_balance", "5000.0")
    assert repo.exists("cash_balance") is True
    assert repo.get("cash_balance") == "5000.0"
    repo.set("cash_balance", "6000.0")
    assert repo.get("cash_balance") == "6000.0"


# --- ArtifactsRepository ---------------------------------------------------


def test_artifacts_json_roundtrip(tmp_path):
    repo = ArtifactsRepository()
    path = tmp_path / "data.json"
    assert repo.read_json(path, default=[]) == []
    repo.write_json(path, {"a": 1})
    assert repo.read_json(path) == {"a": 1}


def test_artifacts_csv_append_and_read(tmp_path):
    repo = ArtifactsRepository()
    path = tmp_path / "rows.csv"
    fields = ["ticker", "price"]
    repo.append_csv_row(path, fields, {"ticker": "AAPL", "price": "100"})
    repo.append_csv_row(path, fields, {"ticker": "MSFT", "price": "200"})
    rows = repo.read_csv_dicts(path)
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT"]


# --- AlertsRepository ------------------------------------------------------


@pytest.fixture
def alerts_repo(tmp_path):
    repo = AlertsRepository(db.make_connect(lambda: tmp_path / "alerts.db"))
    repo.ensure_schema()
    return repo


def test_alerts_record_and_watching(alerts_repo):
    assert alerts_repo.has_watching("AAPL") is False
    alerts_repo.record("AAPL", 9, "Stage 2", "summary", 100.0, 90.0)
    assert alerts_repo.has_watching("AAPL") is True
    assert alerts_repo.last_alerted_at("AAPL") is not None
    rows = alerts_repo.watching()
    assert len(rows) == 1
    rowid = rows[0][0]
    alerts_repo.set_status(rowid, "entered")
    assert alerts_repo.has_watching("AAPL") is False


def test_alerts_clear(alerts_repo):
    alerts_repo.record("AAPL", 9, "Stage 2", "summary", 100.0, 90.0)
    alerts_repo.clear()
    assert alerts_repo.last_alerted_at("AAPL") is None


# --- ResultsRepository -----------------------------------------------------


def test_results_save_and_latest_scores(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    repo.save_results(
        [
            (
                "AAPL",
                "2024-01-01 09:00",
                7,
                10,
                9,
                "Stage 2",
                100.0,
                101.0,
                95.0,
                "far",
            ),
            (
                "AAPL",
                "2024-01-02 09:00",
                8,
                11,
                9,
                "Stage 2",
                102.0,
                103.0,
                96.0,
                "near",
            ),
        ]
    )
    assert repo.latest_scores() == {"AAPL": 8}
