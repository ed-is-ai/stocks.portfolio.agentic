"""Tests for the append-only Strategy assignment history (GH-17).

Every assign and clear appends exactly one ``portfolio_strategy_history``
row in the same transaction as the assignment change, and ``init_trades_db``
seeds the history once from assignments made before it existed.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.repositories import db
from app.repositories.portfolio_strategies_repo import PortfolioStrategiesRepository
from app.services import strategy_assignment_service as svc_module
from app.services.strategy_assignment_service import StrategyAssignmentService
from tests.test_strategy_assignment_service import _discovery_result


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "trades.db"
    with closing(sqlite3.connect(path)) as conn:
        db.init_trades_db(conn)
        conn.execute("INSERT INTO portfolios (name, created_at) VALUES ('SIPP', 'now')")
        conn.commit()
    return path


@pytest.fixture
def service(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StrategyAssignmentService:
    monkeypatch.setattr(
        svc_module, "discover_strategies", lambda root: _discovery_result()
    )
    return StrategyAssignmentService(
        PortfolioStrategiesRepository(db.make_connect(lambda: db_path)),
        skills_root=tmp_path / "skills",
        analysis_path=tmp_path / "analysis.json",
    )


def _history(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT portfolio_id, strategy_id, parameters_json, recorded_at "
            "FROM portfolio_strategy_history ORDER BY id"
        ).fetchall()


def test_assign_and_clear_each_append_exactly_one_row(
    service: StrategyAssignmentService, db_path: Path
) -> None:
    assignment = service.assign(1, "alpha")
    assert _history(db_path) == [(1, "alpha", '{"lookback":20}', assignment.updated_at)]

    service.assign(1, "beta")
    service.clear(1)

    rows = _history(db_path)
    assert [(r[0], r[1]) for r in rows] == [(1, "alpha"), (1, "beta"), (1, None)]
    assert rows[-1][2] == "{}"


def test_history_row_commits_with_the_assignment_or_not_at_all(
    service: StrategyAssignmentService, db_path: Path
) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("DROP TABLE portfolio_strategy_history")
        conn.commit()

    with pytest.raises(sqlite3.OperationalError):
        service.assign(1, "alpha")

    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM portfolio_strategies").fetchone() == (
            0,
        )


def test_init_seeds_history_once_from_existing_assignments(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO portfolio_strategies VALUES "
            "(1, 'alpha', '{\"lookback\":20}', '2025-01-01T00:00:00+00:00', "
            "'2025-02-01T00:00:00+00:00')"
        )
        conn.commit()
        db.init_trades_db(conn)
        db.init_trades_db(conn)

    assert _history(db_path) == [
        (1, "alpha", '{"lookback":20}', "2025-02-01T00:00:00+00:00")
    ]


def test_repository_history_is_oldest_first(db_path: Path) -> None:
    repo = PortfolioStrategiesRepository(db.make_connect(lambda: db_path))
    repo.upsert(1, "alpha", {})
    repo.clear(1)

    entries = repo.history(1)

    assert [e.strategy_id for e in entries] == ["alpha", None]
    assert repo.history_revision() == (2, entries[-1].id)


def test_clearing_nothing_records_nothing(db_path: Path) -> None:
    repo = PortfolioStrategiesRepository(db.make_connect(lambda: db_path))

    assert repo.clear(1) is False
    repo.upsert(1, "alpha", {})
    assert repo.clear(1) is True
    assert repo.clear(1) is False

    assert [e.strategy_id for e in repo.history(1)] == ["alpha", None]
