"""Offline benchmark safety and measurements over real SQLite evidence."""

from contextlib import closing
import sqlite3
from typing import Any, cast

import pytest

from app.repositories.evidence_benchmark import (
    benchmark_metadata,
    database_inventory,
    readonly_connect,
    result_component_timings,
)
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from tests.backtest.test_snapshot_coverage_repository import (
    _commit,
    _profile,
    _repo,
    _snapshot,
)


def test_inventory_counts_quoted_tables_and_cannot_write_or_create(tmp_path):
    path = tmp_path / "evidence.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE "odd""table" (value INTEGER)')
        conn.execute('INSERT INTO "odd""table" VALUES (1), (2)')
        conn.commit()
    before = path.read_bytes()
    inventory = database_inventory(path)
    assert inventory["row_counts"] == {'odd"table': 2}
    assert inventory["file_bytes"] == len(before)
    assert inventory["logical_bytes"] == len(before)
    with closing(readonly_connect(path)()) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute('DELETE FROM "odd""table"')
    assert path.read_bytes() == before
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        readonly_connect(missing)
    assert not missing.exists()


def test_profile_benchmark_reports_separate_first_and_warm_samples(tmp_path):
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    snapshot = _snapshot(_profile())
    _commit(repo, snapshot)
    report = cast(
        dict[str, Any],
        result_component_timings(
            path, profile_hash=snapshot.profile.profile_hash, repetitions=2
        ),
    )
    assert report["profile_hash"] == snapshot.profile.profile_hash
    assert report["warm_repetitions"] == 2
    assert "not a cold OS page cache" in report["cache_conditions"]
    for name in ("coverage", "roster", "total_components"):
        assert report["first_use_seconds"][name] >= 0
        samples = report["warm"][name]["samples_seconds"]
        assert len(samples) == 2
        assert report["warm"][name]["p95_seconds"] == max(samples)
    with pytest.raises(ValueError, match="positive"):
        result_component_timings(path, profile_hash="invalid", repetitions=0)
    with pytest.raises(ValueError, match="select a profile"):
        result_component_timings(path)


def test_integrity_rejection_is_reported_without_bypassing_verification(
    tmp_path, monkeypatch
):
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    snapshot = _snapshot(_profile())
    _commit(repo, snapshot)

    def reject(*args, **kwargs):
        raise BacktestIntegrityError("profile detector authority changed")

    monkeypatch.setattr(BacktestRepository, "snapshot_coverage", reject)
    report = cast(
        dict[str, Any],
        result_component_timings(
            path, profile_hash=snapshot.profile.profile_hash, repetitions=1
        ),
    )
    assert report["status"] == "integrity_failure"
    assert len(report["failures"]) == 2
    assert report["failures"][0]["component"] == "coverage"
    assert report["failures"][0]["detail"] == "profile detector authority changed"
    assert "rejection, not successful reads" in report["cache_conditions"]
    monkeypatch.setattr(BacktestRepository, "backtest_result", reject)
    early = cast(
        dict[str, Any], result_component_timings(path, run_id="missing", repetitions=5)
    )
    assert early["requested_warm_repetitions"] == 5
    assert early["warm_repetitions"] == 0
    assert early["warm"] == {}


def test_inventory_resolves_symlink_wal_and_metadata_identifies_inputs(tmp_path):
    target = tmp_path / "target.db"
    alias = tmp_path / "link.db"
    alias.symlink_to(target)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE example (value INTEGER)")
        conn.execute("INSERT INTO example VALUES (1)")
        conn.commit()
        wal_bytes = target.with_name(target.name + "-wal").stat().st_size
        assert wal_bytes > 0
        inventory = database_inventory(alias)
        assert inventory["wal_bytes"] == wal_bytes
        assert inventory["row_counts"] == {"example": 1}
    metadata = benchmark_metadata(code_revision="abc-dirty", snapshot_id="backup-123")
    assert metadata["application_revision"] == "abc-dirty"
    assert metadata["input_snapshot_id"] == "backup-123"
