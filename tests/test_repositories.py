"""Unit tests for the repository layer (temp-file SQLite + artifact files)."""

import pytest

from app.repositories import db
from app.repositories.account_repo import AccountStateRepository
from app.repositories.alerts_repo import AlertsRepository
from app.repositories.artifacts_repo import ArtifactsRepository
from app.repositories.cash_flows_repo import CashFlowsRepository
from app.repositories.fx_rate_cache_repo import FxRateCacheRepository
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


def test_trades_insert_ignore_dedupes_idempotency_key(trades_connect):
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        first = repo.insert_ignore(
            conn, "AAPL", "BUY", 10, 100.0, "01/02/2024", "", "REF1", None, "key-1"
        )
        second = repo.insert_ignore(
            conn, "AAPL", "BUY", 10, 100.0, "01/02/2024", "", "REF1", None, "key-1"
        )
    # Story 1.8, AC #2/#3: the method reports what actually happened rather
    # than leaving the caller to infer success from "no exception raised".
    assert (first, second) == ("inserted", "duplicate")
    assert len(repo.history("AAPL")) == 1


def test_trades_idempotency_keys_are_scoped_per_portfolio(trades_connect):
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(
            conn, "AAPL", "BUY", 10, 100.0, "01/02/2024", "", "REF1", 1, "key-1"
        )
        repo.insert_ignore(
            conn, "MSFT", "BUY", 5, 200.0, "02/02/2024", "", "REF2", 2, "key-2"
        )

    assert repo.idempotency_keys_for_portfolio(1) == {"key-1"}
    assert repo.idempotency_keys_for_portfolio(2) == {"key-2"}
    assert repo.idempotency_keys_for_portfolio(None) == set()


def test_trades_set_ack_writes_and_clears(trades_connect):
    """Story 1.5, AC #7: ``set_ack`` writes/clears ``realised_pnl_ack_at``,
    and only the targeted row is affected (an untouched trade stays
    ``None``)."""
    repo = TradesRepository(trades_connect)
    target_id = repo.insert("AAPL", "SELL", 5, 100.0, "01/02/2024")
    other_id = repo.insert("MSFT", "BUY", 5, 200.0, "02/02/2024")

    with db.session(trades_connect) as conn:
        repo.set_ack(conn, target_id, "2026-08-09T12:00:00+00:00")

    history = {t.id: t for t in repo.history()}
    assert history[target_id].realised_pnl_ack_at == "2026-08-09T12:00:00+00:00"
    assert history[other_id].realised_pnl_ack_at is None

    with db.session(trades_connect) as conn:
        repo.set_ack(conn, target_id, None)

    history = {t.id: t for t in repo.history()}
    assert history[target_id].realised_pnl_ack_at is None


# --- CashFlowsRepository ---------------------------------------------------


def test_cash_flows_insert_ignore_dedupes(trades_connect):
    repo = CashFlowsRepository(trades_connect)
    with db.session(trades_connect) as conn:
        first = repo.insert_ignore(
            conn, "01/02/2024", "DIVIDEND", None, 12.5, "Div", "R1", None, "key-1"
        )
        second = repo.insert_ignore(
            conn, "01/02/2024", "DIVIDEND", None, 12.5, "Div", "R1", None, "key-1"
        )
    assert (first, second) == ("inserted", "duplicate")
    with db.session(trades_connect) as conn:
        count = conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0]
    assert count == 1
    assert repo.idempotency_keys_for_portfolio(None) == {"key-1"}


def test_cash_flows_history_scopes_and_orders(trades_connect):
    repo = CashFlowsRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(conn, "2024-01-01", "DIVIDEND", None, 12.5, "Div", "R1", 1)
        repo.insert_ignore(
            conn, "2024-03-01", "CONTRIBUTION", None, 500, "Top-up", "R2", 1
        )
        repo.insert_ignore(conn, "2024-02-01", "DIVIDEND", None, 8.0, "Div B", "R3", 2)
    # Scoped to portfolio 1, newest date first.
    flows = repo.history(portfolio_id=1)
    assert [f.reference for f in flows] == ["R2", "R1"]
    assert flows[0].flow_type == "CONTRIBUTION"
    # Portfolio 2 sees only its own row.
    assert [f.reference for f in repo.history(portfolio_id=2)] == ["R3"]


# --- PriceCacheRepository --------------------------------------------------


def test_price_cache_upsert_and_load(trades_connect):
    repo = PriceCacheRepository(trades_connect)
    repo.upsert_many({"AAPL": 100.0}, {"AAPL": (130.0, "USD")})
    repo.upsert_many({"AAPL": 110.0})  # update existing
    rows = repo.load_all()
    assert len(rows) == 1
    assert rows[0][0] == "AAPL"
    assert rows[0][1] == 110.0


# --- FxRateCacheRepository (Story 1.2) --------------------------------------


def test_fx_rate_cache_upsert_and_get_many_round_trip(trades_connect):
    """The real SQL (``IN (...)`` placeholder construction, ``ON CONFLICT``
    upsert) round-trips correctly against a real SQLite DB -- Story 1.2's
    service-layer tests only exercise this repository via an in-memory
    fake, so this is the one test that runs the actual SQL."""
    repo = FxRateCacheRepository(trades_connect)

    repo.upsert_many({"2026-01-01": 1.3456, "2026-01-02": 1.36})
    result = repo.get_many(["2026-01-01", "2026-01-02", "2026-01-03"])
    assert result == {"2026-01-01": 1.3456, "2026-01-02": 1.36}
    assert "2026-01-03" not in result

    # Upsert overwrites an existing row rather than erroring/duplicating.
    repo.upsert_many({"2026-01-01": 1.40})
    assert repo.get_many(["2026-01-01"]) == {"2026-01-01": 1.40}

    # A dumb store: an invalid stored value round-trips unfiltered --
    # PortfolioService, not this repository, is responsible for filtering.
    repo.upsert_many({"2026-01-04": -1.0})
    assert repo.get_many(["2026-01-04"]) == {"2026-01-04": -1.0}

    assert repo.get_many([]) == {}


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


def test_results_latest_scores_multi_ticker(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    repo.save_results(
        [
            # AAPL: two rows — later date has score 9
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
                "2024-01-03 09:00",
                9,
                12,
                9,
                "Stage 2",
                104.0,
                105.0,
                97.0,
                "near",
            ),
            # MSFT: two rows — later date has score 6
            (
                "MSFT",
                "2024-01-02 09:00",
                5,
                8,
                7,
                "Stage 1",
                200.0,
                201.0,
                190.0,
                "far",
            ),
            (
                "MSFT",
                "2024-01-04 09:00",
                6,
                9,
                7,
                "Stage 1",
                202.0,
                203.0,
                191.0,
                "near",
            ),
        ]
    )
    # Each ticker must resolve to its own latest row, not a global MAX.
    assert repo.latest_scores() == {"AAPL": 9, "MSFT": 6}


def test_results_latest_scores_empty(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    assert repo.latest_scores() == {}


def test_results_latest_scores_single_row_per_ticker(tmp_path):
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
                "MSFT",
                "2024-01-01 09:00",
                5,
                8,
                7,
                "Stage 1",
                200.0,
                201.0,
                190.0,
                "far",
            ),
        ]
    )
    # Both tickers must appear; a LIMIT 1 or global MAX would drop one.
    assert repo.latest_scores() == {"AAPL": 7, "MSFT": 5}
