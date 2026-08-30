"""Tests for the ``portfolio_strategies`` repository (#440).

Covers the at-most-one-assignment lifecycle: upsert inserts then replaces a
single row, ``assigned_at`` is stable across replaces while ``updated_at``
advances, clear removes the row, portfolio deletion removes the assignment,
and the stored parameter snapshot is canonical (sorted keys, compact).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.repositories import db
from app.repositories.portfolio_strategies_repo import (
    PortfolioStrategiesRepository,
)
from app.repositories.portfolios_repo import PortfoliosRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create an initialised trades.db with one portfolio, return its path."""
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.execute("INSERT INTO portfolios (name, created_at) VALUES ('SIPP', 'now')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def repo(db_path: Path) -> PortfolioStrategiesRepository:
    return PortfolioStrategiesRepository(db.make_connect(lambda: db_path))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Replace the repo's clock with a deterministic advancing sequence."""
    ticks = iter(
        [
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
        ]
    )
    seen: list[str] = []
    monkeypatch.setattr(
        "app.repositories.portfolio_strategies_repo._utc_now",
        lambda: seen.append(next(ticks)) or seen[-1],
    )
    yield seen


def test_upsert_inserts_then_replaces_single_row(
    repo: PortfolioStrategiesRepository, db_path: Path
) -> None:
    first = repo.upsert(1, "alpha", {"lookback": 20})
    assert first.strategy_id == "alpha"
    assert first.parameters == {"lookback": 20}

    second = repo.upsert(1, "beta", {"lookback": 50})
    assert second.strategy_id == "beta"
    assert second.parameters == {"lookback": 50}

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT portfolio_id, strategy_id FROM portfolio_strategies")
    assert rows.fetchall() == [(1, "beta")]
    conn.close()


def test_assigned_at_stable_and_updated_at_advances(
    repo: PortfolioStrategiesRepository, clock: list[str]
) -> None:
    first = repo.upsert(1, "alpha", {"lookback": 20})
    second = repo.upsert(1, "beta", {"lookback": 50})

    assert first.assigned_at == "2026-01-01T00:00:00+00:00"
    # assigned_at is preserved across the replace; updated_at advances.
    assert second.assigned_at == first.assigned_at
    assert second.updated_at == "2026-01-02T00:00:00+00:00"
    assert second.updated_at > first.updated_at


def test_clear_removes_row_and_get_returns_none(
    repo: PortfolioStrategiesRepository,
) -> None:
    repo.upsert(1, "alpha", {"lookback": 20})
    assert repo.get(1) is not None
    assert repo.clear(1) is True
    assert repo.get(1) is None
    # Clearing again is idempotent.
    assert repo.clear(1) is False


def test_portfolio_delete_removes_assignment(
    repo: PortfolioStrategiesRepository, db_path: Path
) -> None:
    repo.upsert(1, "alpha", {"lookback": 20})
    portfolios = PortfoliosRepository(db.make_connect(lambda: db_path))
    assert portfolios.delete(1) is True
    assert repo.get(1) is None
    assert repo.list_assigned() == []


def test_list_assigned_returns_all_portfolios_ordered(
    repo: PortfolioStrategiesRepository, db_path: Path
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO portfolios (name, created_at) VALUES ('ISA', 'now')")
    conn.commit()
    conn.close()
    repo.upsert(2, "beta", {"lookback": 5})
    repo.upsert(1, "alpha", {"lookback": 20})
    assert [a.portfolio_id for a in repo.list_assigned()] == [1, 2]


def test_parameters_json_is_canonical(
    repo: PortfolioStrategiesRepository, db_path: Path
) -> None:
    repo.upsert(1, "alpha", {"b": 2, "a": 1, "c": None})
    conn = sqlite3.connect(db_path)
    raw = conn.execute(
        "SELECT parameters_json FROM portfolio_strategies WHERE portfolio_id = 1"
    ).fetchone()[0]
    conn.close()
    assert raw == '{"a":1,"b":2,"c":null}'
