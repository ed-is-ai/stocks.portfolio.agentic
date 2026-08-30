"""Tests for the recommendation-email dispatch receipt repository (#442).

The composite PK ``(portfolio_id, analysis_run_id)`` is the send-authority:
claim is atomic, a second claim for the same pair is refused, and a later
run claims fresh.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.repositories import db
from app.repositories.portfolio_dispatch_repo import PortfolioDispatchRepository


def _repo(db_path: Path) -> PortfolioDispatchRepository:
    return PortfolioDispatchRepository(db.make_connect(lambda: db_path))


def _status(db_path: Path, portfolio_id: int, run_id: str) -> tuple[str, str | None]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, completed_at FROM portfolio_recommendation_dispatches "
            "WHERE portfolio_id = ? AND analysis_run_id = ?",
            (portfolio_id, run_id),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), row[1]


def test_claim_inserts_and_returns_true(tmp_path: Path) -> None:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.close()
    repo = _repo(path)
    assert repo.claim(7, "run-1", "alpha") is True
    assert _status(path, 7, "run-1")[0] == "claimed"


def test_second_claim_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.close()
    repo = _repo(path)
    assert repo.claim(7, "run-1", "alpha") is True
    assert repo.claim(7, "run-1", "alpha") is False


def test_mark_sent_sets_status_and_completed_at(tmp_path: Path) -> None:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.close()
    repo = _repo(path)
    repo.claim(7, "run-1", "alpha")
    assert repo.was_sent(7, "run-1") is False
    repo.mark_sent(7, "run-1")
    status, completed_at = _status(path, 7, "run-1")
    assert status == "sent"
    assert completed_at is not None
    assert repo.was_sent(7, "run-1") is True


def test_mark_failed_sets_status(tmp_path: Path) -> None:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.close()
    repo = _repo(path)
    repo.claim(7, "run-1", "alpha")
    repo.mark_failed(7, "run-1")
    status, completed_at = _status(path, 7, "run-1")
    assert status == "failed"
    assert completed_at is not None
    assert repo.was_sent(7, "run-1") is False


def test_new_run_claims_fresh(tmp_path: Path) -> None:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.close()
    repo = _repo(path)
    repo.claim(7, "run-1", "alpha")
    repo.mark_sent(7, "run-1")
    # A later analysis run claims a fresh slot for the same portfolio.
    assert repo.claim(7, "run-2", "alpha") is True
    assert repo.was_sent(7, "run-1") is True
    assert repo.was_sent(7, "run-2") is False
