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


def test_roster_commit_accepts_cboe_bzx_mic(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    commit = _commit()
    security_id = commit.identities[0][0]
    bats_commit = replace(
        commit,
        identities=((security_id, "BATS", "CBOE", "d" * 64),),
        members=(
            (
                security_id,
                "BATS",
                "CBOE",
                "USD",
                '["datahub_sp500"]',
                '[{"mic":"BATS"}]',
                "m" * 64,
            ),
        ),
    )

    assert repo.commit_roster_capture(bats_commit) == commit.roster_digest


def test_ensure_schema_expands_legacy_mic_constraints(tmp_path) -> None:
    path = tmp_path / "backtest.db"
    conn = db.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE security_identity_registry_revisions (
                revision_digest TEXT PRIMARY KEY,
                canonical_manifest_json TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE security_identities (
                security_id TEXT PRIMARY KEY,
                mic TEXT NOT NULL CHECK(mic IN ('XNAS', 'XNYS', 'XLON')),
                provider_symbol TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                identity_registry_revision TEXT NOT NULL
                    REFERENCES security_identity_registry_revisions(revision_digest),
                created_at TEXT NOT NULL,
                UNIQUE(mic, provider_symbol)
            );
            INSERT INTO security_identity_registry_revisions
                VALUES ('revision', '{}', 'evidence', '2026-08-10T12:00:00+00:00');
            INSERT INTO security_identities VALUES
                ('security', 'XNAS', 'AAPL', 'digest', 'revision',
                 '2026-08-10T12:00:00+00:00');
            CREATE TABLE identity_audit (security_id TEXT NOT NULL);
            CREATE TRIGGER identity_audit_requires_security
            BEFORE INSERT ON identity_audit
            WHEN NOT EXISTS (
                SELECT 1 FROM security_identities
                WHERE security_id = NEW.security_id
            )
            BEGIN SELECT RAISE(ABORT, 'security is missing'); END;
            CREATE TABLE security_identities__bats_migration AS
                SELECT * FROM security_identities WHERE 0;
            """
        )
        conn.commit()
    finally:
        conn.close()

    repo = BacktestRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    conn = db.connect(path)
    try:
        schema = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='security_identities'"
            ).fetchone()[0]
        )
        assert "'BATS'" in schema
        assert conn.execute(
            "SELECT security_id, mic, provider_symbol FROM security_identities"
        ).fetchall() == [("security", "XNAS", "AAPL")]
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name='identity_audit_requires_security'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE '%bats_migration%'"
        ).fetchone() is None
        conn.execute("INSERT INTO identity_audit VALUES ('security')")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
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
