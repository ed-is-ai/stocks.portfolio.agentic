"""Unit tests for ``TickerCurrencyCacheRepository``."""

from pathlib import Path

import pytest

from app.repositories import db
from app.repositories.ticker_currency_cache_repo import TickerCurrencyCacheRepository


@pytest.fixture
def repo(tmp_path: Path) -> TickerCurrencyCacheRepository:
    path = tmp_path / "trades.db"
    connect = db.make_connect(lambda: path)
    with db.session(connect) as conn:
        db.init_trades_db(conn)
    return TickerCurrencyCacheRepository(connect)


def test_get_many_returns_only_stored_tickers(
    repo: TickerCurrencyCacheRepository,
) -> None:
    repo.upsert_many({"AAPL": "USD"})

    assert repo.get_many(["AAPL", "UNKNOWN"]) == {"AAPL": "USD"}


def test_get_many_with_no_tickers_returns_empty(
    repo: TickerCurrencyCacheRepository,
) -> None:
    assert repo.get_many([]) == {}


def test_upsert_many_with_empty_dict_is_a_no_op(
    repo: TickerCurrencyCacheRepository,
) -> None:
    repo.upsert_many({})

    assert repo.get_many(["AAPL"]) == {}


def test_upsert_many_overwrites_existing_row(
    repo: TickerCurrencyCacheRepository,
) -> None:
    repo.upsert_many({"AAPL": "GBP"})
    repo.upsert_many({"AAPL": "USD"})

    assert repo.get_many(["AAPL"]) == {"AAPL": "USD"}
    with db.session(repo._connect) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM ticker_currency_cache WHERE ticker = ?",
            ("AAPL",),
        ).fetchone()[0]
    assert count == 1


def test_upsert_many_persists_multiple_tickers_independently(
    repo: TickerCurrencyCacheRepository,
) -> None:
    repo.upsert_many({"AAPL": "USD", "VOD.L": "GBP", "0700.HK": "HKD"})

    assert repo.get_many(["AAPL", "VOD.L", "0700.HK"]) == {
        "AAPL": "USD",
        "VOD.L": "GBP",
        "0700.HK": "HKD",
    }
