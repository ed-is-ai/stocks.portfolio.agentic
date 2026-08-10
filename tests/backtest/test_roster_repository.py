from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository, RosterCaptureCommit


NOW = datetime(2026, 8, 10, 12, tzinfo=timezone.utc).isoformat()


def _commit(
    lineage: str = "lineage-1", roster_digest: str = "r" * 64
) -> RosterCaptureCommit:
    return RosterCaptureCommit(
        lineage_id=lineage,
        roster_digest=roster_digest,
        roster_manifest_json='{"schema_version":"ReconstructionRosterManifestV1"}',
        policy_version="ReconstructionRosterPolicyV1",
        identity_registry_revision="i" * 64,
        identity_registry_json='{"identities":[]}',
        identity_evidence_digest="e" * 64,
        alias_revision="a" * 64,
        alias_manifest_json='{"entries":[]}',
        alias_evidence_digest="b" * 64,
        captured_at=NOW,
        identities=(
            (
                "7d16e313-2dd2-45a8-8a33-7b61b7df3fc8",
                "XNAS",
                "AAPL",
                "d" * 64,
            ),
        ),
        aliases=(),
        sources=(("datahub_sp500", "p" * 64, '[{"symbol":"AAPL"}]', NOW),),
        members=(
            (
                "7d16e313-2dd2-45a8-8a33-7b61b7df3fc8",
                "XNAS",
                "AAPL",
                "USD",
                '["datahub_sp500"]',
                '[{"mic":"XNAS"}]',
                "m" * 64,
            ),
        ),
    )


def test_roster_commit_is_atomic_immutable_and_reused_by_lineage(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    commit = _commit()

    assert repo.commit_roster_capture(commit) == commit.roster_digest
    assert repo.commit_roster_capture(commit) == commit.roster_digest
    assert repo.roster_digest_for_lineage("lineage-1") == commit.roster_digest

    with pytest.raises(sqlite3.IntegrityError, match="different roster"):
        repo.commit_roster_capture(_commit(roster_digest="x" * 64))

    conn = repo._connect()  # repository immutability is enforced by SQLite
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE reconstruction_rosters SET policy_version='bad'")
    finally:
        conn.close()


def test_backtest_connections_enforce_foreign_keys(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()

    conn = repo._connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO reconstruction_roster_lineages
                   (lineage_id, roster_digest, bound_at) VALUES (?, ?, ?)""",
                ("invalid", "missing", NOW),
            )
    finally:
        conn.close()


def test_conflicting_identity_rolls_back_entire_capture(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    repo.commit_roster_capture(_commit())
    conflict = _commit(lineage="lineage-2", roster_digest="z" * 64)
    conflict = replace(conflict, identities=(("another-id", "XNAS", "AAPL", "q" * 64),))
    with pytest.raises(sqlite3.IntegrityError):
        repo.commit_roster_capture(conflict)
    assert repo.roster_digest_for_lineage("lineage-2") is None
