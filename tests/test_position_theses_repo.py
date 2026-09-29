"""Tests for the position-thesis repository and its DB constraints (GH-14)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.agents.thesis.evaluator import evaluate_thesis
from app.repositories import db
from app.repositories.position_theses_repo import (
    PositionThesesRepository,
    StaleDraftError,
    VersionConflictError,
)
from app.schemas.position_thesis import ThesisContentV1
from tests.test_thesis_evaluator import FRESH, META, make_record

CONTENT = ThesisContentV1.model_validate(
    {
        "rationale": "Stage 2 leader.",
        "expected_setup": "Holds its 50-day SMA.",
        "rules": [{"kind": "close_below_sma", "period": 50}],
    }
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "trades.db"
    with closing(db.connect(path)) as conn:
        db.init_trades_db(conn)
        conn.execute(
            "INSERT INTO portfolios (id, name, created_at) VALUES (7, 'SIPP', 'n')"
        )
        conn.commit()
    return path


@pytest.fixture
def repo(db_path: Path) -> PositionThesesRepository:
    return PositionThesesRepository(db.make_connect(lambda: db_path))


def _active_rows(db_path: Path) -> list[tuple[int, int]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT id, version FROM position_theses WHERE active = 1"
        ).fetchall()


_INSERT = (
    "INSERT INTO position_theses (portfolio_id, security_id, version, "
    "rationale, expected_setup, rules_json, text_source, active, "
    "created_at) VALUES (7, ?, ?, 'r', 's', '[]', 'user', ?, 'n')"
)


def test_activating_two_versions_in_turn_leaves_exactly_one_active(
    repo: PositionThesesRepository, db_path: Path
) -> None:
    first = repo.add_version(7, "AAA", CONTENT, "ai_draft", active=False)
    assert _active_rows(db_path) == []
    repo.activate_pending_draft(7, "AAA", first.id)
    assert _active_rows(db_path) == [(first.id, 1)]
    second = repo.add_version(7, "AAA", CONTENT, "ai_draft", active=False)
    activated = repo.activate_pending_draft(7, "AAA", second.id)

    assert (first.version, second.version) == (1, 2)

    assert activated is not None and activated.active and activated.confirmed_at
    assert _active_rows(db_path) == [(second.id, 2)]


def test_partial_unique_index_rejects_a_second_active_row(
    repo: PositionThesesRepository, db_path: Path
) -> None:
    active = repo.add_version(7, "AAA", CONTENT, "user", active=True)
    with closing(sqlite3.connect(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, ("AAA", 9, 1))
        # An inactive AAA version beside the active one, and another
        # holding's active version, are both allowed.
        conn.execute(_INSERT, ("AAA", 10, 0))
        other = conn.execute(_INSERT, ("BBB", 1, 1)).lastrowid
        conn.commit()

    assert sorted(_active_rows(db_path)) == sorted([(active.id, 1), (other, 1)])
    with closing(sqlite3.connect(db_path)) as conn:
        inactive = conn.execute(
            "SELECT version FROM position_theses "
            "WHERE security_id = 'AAA' AND active = 0"
        ).fetchall()
    assert inactive == [(10,)]


def test_user_save_supersedes_the_active_version(
    repo: PositionThesesRepository,
) -> None:
    repo.add_version(7, "AAA", CONTENT, "user", active=True)
    second = repo.add_version(7, "AAA", CONTENT, "user", active=True)

    assert repo.active_for_portfolio(7) == {"AAA": second}
    assert second.text_source == "user" and second.confirmed_at


def test_pending_draft_is_the_newest_unconfirmed_ai_version(
    repo: PositionThesesRepository,
) -> None:
    draft = repo.add_version(7, "AAA", CONTENT, "ai_draft", active=False)
    assert repo.pending_drafts(7) == {"AAA": draft}
    assert draft.confirmed_at is None and not draft.active

    repo.add_version(7, "AAA", CONTENT, "user", active=True)
    assert repo.pending_drafts(7) == {}


def test_evaluations_append_once_per_run_and_read_back(
    repo: PositionThesesRepository,
) -> None:
    thesis = repo.add_version(7, "AAA", CONTENT, "user", active=True)
    evaluation = evaluate_thesis(thesis, make_record(price=94.0), META, FRESH)

    assert repo.append_evaluation(evaluation) is True
    assert repo.append_evaluation(evaluation) is False
    assert repo.latest_evaluations([thesis.id]) == {thesis.id: evaluation}
    assert repo.latest_evaluations([]) == {}
    assert repo.history(7, "AAA") == [evaluation]


def test_history_keeps_the_last_ten(repo: PositionThesesRepository) -> None:
    thesis = repo.add_version(7, "AAA", CONTENT, "user", active=True)
    for n in range(12):
        meta = META.model_copy(update={"run_id": f"run-{n}"})
        repo.append_evaluation(evaluate_thesis(thesis, make_record(), meta, FRESH))

    history = repo.history(7, "AAA")

    assert len(history) == 10
    assert history[0].analysis_run_id == "run-11"


def test_deleting_the_portfolio_cascades(
    repo: PositionThesesRepository, db_path: Path
) -> None:
    thesis = repo.add_version(7, "AAA", CONTENT, "user", active=True)
    repo.append_evaluation(evaluate_thesis(thesis, make_record(), META, FRESH))
    with closing(db.connect(db_path)) as conn:
        conn.execute("DELETE FROM portfolios WHERE id = 7")
        conn.commit()
        counts = [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("position_theses", "thesis_evaluations")
        ]

    assert counts == [0, 0]


def test_confirming_a_draft_superseded_by_a_save_is_stale(
    repo: PositionThesesRepository, db_path: Path
) -> None:
    draft = repo.add_version(7, "AAA", CONTENT, "ai_draft", active=False)
    saved = repo.add_version(7, "AAA", CONTENT, "user", active=True)

    with pytest.raises(StaleDraftError):
        repo.activate_pending_draft(7, "AAA", draft.id)
    with pytest.raises(StaleDraftError):  # the wrong holding is stale too
        repo.activate_pending_draft(7, "BBB", draft.id)
    assert _active_rows(db_path) == [(saved.id, 2)]


def test_corrupt_rows_are_skipped_not_raised(
    repo: PositionThesesRepository, db_path: Path
) -> None:
    good = repo.add_version(7, "AAA", CONTENT, "user", active=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO position_theses (portfolio_id, security_id, version, "
            "rationale, expected_setup, rules_json, text_source, active, "
            "created_at) VALUES "
            "(7, 'BBB', 1, 'r', 's', 'not json', 'user', 1, 'n'), "
            "(7, 'CCC', 1, 'r', 's', 'not json', 'ai_draft', 0, 'n')"
        )
        conn.commit()

    assert repo.active_for_portfolio(7) == {"AAA": good}
    assert repo.pending_drafts(7) == {}


def test_insert_expecting_an_older_newest_version_writes_nothing(
    repo: PositionThesesRepository, db_path: Path
) -> None:
    saved = repo.add_version(7, "AAA", CONTENT, "user", active=True)

    with pytest.raises(VersionConflictError):
        repo.add_version(
            7, "AAA", CONTENT, "ai_draft", active=False, expected_latest_version=0
        )
    assert repo.latest_version(7, "AAA") == 1
    assert repo.pending_drafts(7) == {}

    draft = repo.add_version(
        7, "AAA", CONTENT, "ai_draft", active=False, expected_latest_version=1
    )
    assert draft.version == 2
    assert _active_rows(db_path) == [(saved.id, 1)]
