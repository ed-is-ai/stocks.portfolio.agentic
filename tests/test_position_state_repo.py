"""Unit tests for the trailing-stop high-water-mark repository (#82)."""

from app.repositories import db
from app.repositories.position_state_repo import (
    PositionStateRepository,
    build_position_state_repository,
)


def _repo(tmp_path) -> PositionStateRepository:
    connect = db.make_connect(lambda: str(tmp_path / "position_state.db"))
    repo = PositionStateRepository(connect)
    repo.ensure_schema()
    return repo


def test_get_hwm_missing_returns_none(tmp_path) -> None:
    assert _repo(tmp_path).get_hwm("NVDA") is None


def test_record_high_rises_and_never_decreases(tmp_path) -> None:
    repo = _repo(tmp_path)
    assert repo.record_high("NVDA", 100.0) == 100.0
    assert repo.record_high("NVDA", 120.0) == 120.0  # new high
    assert repo.record_high("NVDA", 90.0) == 120.0  # dip leaves peak intact
    assert repo.get_hwm("NVDA") == 120.0


def test_hwm_persists_across_repository_instances(tmp_path) -> None:
    _repo(tmp_path).record_high("AMD", 200.0)
    # A fresh repository/connection against the same file sees the peak.
    assert _repo(tmp_path).get_hwm("AMD") == 200.0


def test_prune_removes_untracked_tickers(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.record_high("NVDA", 100.0)
    repo.record_high("AMD", 200.0)
    repo.record_high("TSLA", 300.0)

    removed = repo.prune({"NVDA"})

    assert removed == 2
    assert repo.get_hwm("NVDA") == 100.0
    assert repo.get_hwm("AMD") is None
    assert repo.get_hwm("TSLA") is None


def test_build_factory_reads_configured_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.config.POSITION_STATE_DB", tmp_path / "position_state.db"
    )
    repo = build_position_state_repository()
    repo.record_high("NVDA", 100.0)
    assert build_position_state_repository().get_hwm("NVDA") == 100.0
