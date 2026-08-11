"""Persistence for immutable Strategy Manager evidence and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import sqlite3

from app.repositories.db import Connect, session
from app.services.backtest.historical_scan_record import (
    DetectorFragmentEnvelopeV1,
    HistoricalScanContractError,
)

_QUALIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_source_qualifications (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_digest      TEXT NOT NULL,
    source_versions_json TEXT NOT NULL,
    fixture_digest       TEXT NOT NULL,
    probe_definition_digest TEXT NOT NULL,
    probe_digest         TEXT NOT NULL,
    qualified_at         TEXT NOT NULL,
    passed               INTEGER NOT NULL CHECK(passed IN (0, 1)),
    failure_code         TEXT,
    failure_reason       TEXT,
    CHECK(
        (passed = 1 AND failure_code IS NULL AND failure_reason IS NULL)
        OR
        (passed = 0 AND failure_code IS NOT NULL AND failure_reason IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_source_qualification_contract
ON historical_source_qualifications(contract_digest, id DESC);
CREATE TRIGGER IF NOT EXISTS qualification_append_only_update
BEFORE UPDATE ON historical_source_qualifications
BEGIN SELECT RAISE(ABORT, 'qualification evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS qualification_append_only_delete
BEFORE DELETE ON historical_source_qualifications
BEGIN SELECT RAISE(ABORT, 'qualification evidence is append-only'); END;
"""

_ROSTER_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS security_identity_registry_revisions (
    revision_digest TEXT PRIMARY KEY,
    canonical_manifest_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_identities (
    security_id TEXT PRIMARY KEY,
    mic TEXT NOT NULL CHECK(mic IN ('XNAS', 'XNYS', 'XLON')),
    provider_symbol TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    identity_registry_revision TEXT NOT NULL REFERENCES security_identity_registry_revisions(revision_digest),
    created_at TEXT NOT NULL,
    UNIQUE(mic, provider_symbol)
);
CREATE TABLE IF NOT EXISTS security_alias_manifests (
    alias_revision TEXT PRIMARY KEY,
    canonical_manifest_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_alias_entries (
    alias_revision TEXT NOT NULL REFERENCES security_alias_manifests(alias_revision),
    security_id TEXT NOT NULL REFERENCES security_identities(security_id),
    provider TEXT NOT NULL,
    mic TEXT NOT NULL CHECK(mic IN ('XNAS', 'XNYS', 'XLON')),
    observed_symbol TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    evidence_source TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('provider_evidence', 'manual_override')),
    PRIMARY KEY(alias_revision, provider, mic, observed_symbol, effective_from, effective_to, security_id),
    CHECK(effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to)
);
CREATE TABLE IF NOT EXISTS reconstruction_rosters (
    roster_digest TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    canonical_manifest_json TEXT NOT NULL,
    identity_registry_revision TEXT NOT NULL REFERENCES security_identity_registry_revisions(revision_digest),
    alias_revision TEXT NOT NULL REFERENCES security_alias_manifests(alias_revision),
    captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconstruction_roster_sources (
    roster_digest TEXT NOT NULL REFERENCES reconstruction_rosters(roster_digest),
    source_name TEXT NOT NULL CHECK(source_name IN ('datahub_sp500', 'tradingview_us', 'tradingview_uk')),
    payload_digest TEXT NOT NULL,
    original_payload_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_order INTEGER NOT NULL,
    PRIMARY KEY(roster_digest, source_name),
    UNIQUE(roster_digest, source_order)
);
CREATE TABLE IF NOT EXISTS reconstruction_roster_members (
    roster_digest TEXT NOT NULL REFERENCES reconstruction_rosters(roster_digest),
    security_id TEXT NOT NULL REFERENCES security_identities(security_id),
    mic TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    currency TEXT NOT NULL,
    source_memberships_json TEXT NOT NULL,
    identity_evidence_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    PRIMARY KEY(roster_digest, security_id),
    UNIQUE(roster_digest, mic, provider_symbol)
);
CREATE TABLE IF NOT EXISTS reconstruction_roster_lineages (
    lineage_id TEXT PRIMARY KEY,
    roster_digest TEXT NOT NULL REFERENCES reconstruction_rosters(roster_digest),
    bound_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS alias_no_overlap
BEFORE INSERT ON security_alias_entries
WHEN EXISTS (
    SELECT 1 FROM security_alias_entries existing
    WHERE existing.provider = NEW.provider
      AND existing.alias_revision = NEW.alias_revision
      AND existing.mic = NEW.mic
      AND existing.observed_symbol = NEW.observed_symbol
      AND COALESCE(existing.effective_from, '0001-01-01') < COALESCE(NEW.effective_to, '9999-12-31')
      AND COALESCE(NEW.effective_from, '0001-01-01') < COALESCE(existing.effective_to, '9999-12-31')
)
BEGIN SELECT RAISE(ABORT, 'alias intervals overlap'); END;

CREATE TRIGGER IF NOT EXISTS identity_registry_immutable_update BEFORE UPDATE ON security_identity_registry_revisions BEGIN SELECT RAISE(ABORT, 'identity registry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS identity_registry_immutable_delete BEFORE DELETE ON security_identity_registry_revisions BEGIN SELECT RAISE(ABORT, 'identity registry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS security_identity_immutable_update BEFORE UPDATE ON security_identities BEGIN SELECT RAISE(ABORT, 'security identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS security_identity_immutable_delete BEFORE DELETE ON security_identities BEGIN SELECT RAISE(ABORT, 'security identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS alias_manifest_immutable_update BEFORE UPDATE ON security_alias_manifests BEGIN SELECT RAISE(ABORT, 'alias manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS alias_manifest_immutable_delete BEFORE DELETE ON security_alias_manifests BEGIN SELECT RAISE(ABORT, 'alias manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS alias_entry_immutable_update BEFORE UPDATE ON security_alias_entries BEGIN SELECT RAISE(ABORT, 'alias entry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS alias_entry_immutable_delete BEFORE DELETE ON security_alias_entries BEGIN SELECT RAISE(ABORT, 'alias entry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_immutable_update BEFORE UPDATE ON reconstruction_rosters BEGIN SELECT RAISE(ABORT, 'reconstruction roster is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_immutable_delete BEFORE DELETE ON reconstruction_rosters BEGIN SELECT RAISE(ABORT, 'reconstruction roster is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_source_immutable_update BEFORE UPDATE ON reconstruction_roster_sources BEGIN SELECT RAISE(ABORT, 'roster source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_source_immutable_delete BEFORE DELETE ON reconstruction_roster_sources BEGIN SELECT RAISE(ABORT, 'roster source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_member_immutable_update BEFORE UPDATE ON reconstruction_roster_members BEGIN SELECT RAISE(ABORT, 'roster member is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_member_immutable_delete BEFORE DELETE ON reconstruction_roster_members BEGIN SELECT RAISE(ABORT, 'roster member is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_lineage_immutable_update BEFORE UPDATE ON reconstruction_roster_lineages BEGIN SELECT RAISE(ABORT, 'roster lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_lineage_immutable_delete BEFORE DELETE ON reconstruction_roster_lineages BEGIN SELECT RAISE(ABORT, 'roster lineage is immutable'); END;
"""

_SCAN_RECONSTRUCTION_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_reconstruction_cache (
    security_id       TEXT NOT NULL,
    date              TEXT NOT NULL,
    detector          TEXT NOT NULL CHECK(detector IN (
        'technical_indicators_v1', 'weinstein_stage_v1', 'vcp_v1'
    )),
    detector_version  TEXT NOT NULL CHECK(length(detector_version) = 64),
    input_revision    TEXT NOT NULL CHECK(length(input_revision) = 64),
    scan_result_json  TEXT NOT NULL,
    scan_result_digest TEXT NOT NULL CHECK(length(scan_result_digest) = 64),
    PRIMARY KEY (security_id, date, detector, detector_version, input_revision)
);
CREATE TRIGGER IF NOT EXISTS scan_reconstruction_cache_immutable_update
BEFORE UPDATE ON scan_reconstruction_cache
BEGIN SELECT RAISE(ABORT, 'scan reconstruction cache is immutable'); END;
CREATE TRIGGER IF NOT EXISTS scan_reconstruction_cache_immutable_delete
BEFORE DELETE ON scan_reconstruction_cache
BEGIN SELECT RAISE(ABORT, 'scan reconstruction cache is immutable'); END;
"""


@dataclass(frozen=True)
class QualificationResult:
    contract_digest: str
    source_versions_json: str
    fixture_digest: str
    probe_definition_digest: str
    probe_digest: str
    qualified_at: str
    passed: bool
    failure_code: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class RosterCaptureCommit:
    """Complete atomic write-set for one immutable roster-lineage binding."""

    lineage_id: str
    roster_digest: str
    roster_manifest_json: str
    policy_version: str
    identity_registry_revision: str
    identity_registry_json: str
    identity_evidence_digest: str
    alias_revision: str
    alias_manifest_json: str
    alias_evidence_digest: str
    captured_at: str
    identities: tuple[tuple[str, str, str, str], ...]
    aliases: tuple[
        tuple[
            str,
            str,
            str,
            str,
            str | None,
            str | None,
            str,
            str,
            str,
        ],
        ...,
    ]
    sources: tuple[tuple[str, str, str, str], ...]
    members: tuple[tuple[str, str, str, str, str, str, str], ...]


class BacktestIntegrityError(RuntimeError):
    code = "integrity_error"


@dataclass(frozen=True)
class DetectorCacheKey:
    security_id: str
    date: date
    detector: str
    detector_version: str
    input_revision: str

    def sql_values(self) -> tuple[str, str, str, str, str]:
        return (
            self.security_id,
            self.date.isoformat(),
            self.detector,
            self.detector_version,
            self.input_revision,
        )


def _row_to_result(row: tuple[object, ...]) -> QualificationResult:
    return QualificationResult(
        contract_digest=str(row[0]),
        source_versions_json=str(row[1]),
        fixture_digest=str(row[2]),
        probe_definition_digest=str(row[3]),
        probe_digest=str(row[4]),
        qualified_at=str(row[5]),
        passed=bool(row[6]),
        failure_code=None if row[7] is None else str(row[7]),
        failure_reason=None if row[8] is None else str(row[8]),
    )


class BacktestRepository:
    """Repository seed that later stories extend with jobs and results."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        with session(self._connect) as conn:
            conn.executescript(
                _QUALIFICATION_SCHEMA
                + _ROSTER_SCHEMA
                + _SCAN_RECONSTRUCTION_CACHE_SCHEMA
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(historical_source_qualifications)"
                ).fetchall()
            }
            if "probe_definition_digest" not in columns:
                try:
                    conn.execute(
                        "ALTER TABLE historical_source_qualifications "
                        "ADD COLUMN probe_definition_digest TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError:
                    raise

    def record_qualification(self, result: QualificationResult) -> int:
        with session(self._connect) as conn:
            cursor = conn.execute(
                """INSERT INTO historical_source_qualifications (
                    contract_digest, source_versions_json, fixture_digest,
                    probe_definition_digest, probe_digest, qualified_at, passed,
                    failure_code, failure_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.contract_digest,
                    result.source_versions_json,
                    result.fixture_digest,
                    result.probe_definition_digest,
                    result.probe_digest,
                    result.qualified_at,
                    int(result.passed),
                    result.failure_code,
                    result.failure_reason,
                ),
            )
            return int(cursor.lastrowid or 0)

    def qualification_history(self, contract_digest: str) -> list[QualificationResult]:
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT contract_digest, source_versions_json, fixture_digest,
                          probe_definition_digest, probe_digest, qualified_at, passed, failure_code,
                          failure_reason
                   FROM historical_source_qualifications
                   WHERE contract_digest = ? ORDER BY id ASC""",
                (contract_digest,),
            ).fetchall()
        return [_row_to_result(row) for row in rows]

    def latest_qualification(self, contract_digest: str) -> QualificationResult | None:
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT contract_digest, source_versions_json, fixture_digest,
                          probe_definition_digest, probe_digest, qualified_at, passed, failure_code,
                          failure_reason
                   FROM historical_source_qualifications
                   WHERE contract_digest = ?
                   ORDER BY id DESC LIMIT 1""",
                (contract_digest,),
            ).fetchone()
        return None if row is None else _row_to_result(row)

    def roster_digest_for_lineage(self, lineage_id: str) -> str | None:
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT roster_digest FROM reconstruction_roster_lineages WHERE lineage_id=?",
                (lineage_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def roster_manifest_json(self, roster_digest: str) -> str | None:
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT canonical_manifest_json FROM reconstruction_rosters WHERE roster_digest=?",
                (roster_digest,),
            ).fetchone()
        return None if row is None else str(row[0])

    def identity_rows(self) -> list[tuple[str, str, str, str]]:
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT security_id, mic, provider_symbol, evidence_digest
                   FROM security_identities ORDER BY security_id"""
            ).fetchall()
        return [tuple(str(value) for value in row) for row in rows]  # type: ignore[return-value]

    def compare_and_insert_detector_fragment(
        self, key: DetectorCacheKey, canonical_json: str | bytes
    ) -> DetectorFragmentEnvelopeV1:
        raw = (
            canonical_json.encode("utf-8")
            if isinstance(canonical_json, str)
            else bytes(canonical_json)
        )
        try:
            envelope = DetectorFragmentEnvelopeV1.from_canonical_json(raw)
        except HistoricalScanContractError as exc:
            raise BacktestIntegrityError("detector fragment is not canonical") from exc
        self._verify_fragment_key(key, envelope)
        rendered = raw.decode("utf-8")
        digest = sha256(raw).hexdigest()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR IGNORE INTO scan_reconstruction_cache (
                       security_id, date, detector, detector_version, input_revision,
                       scan_result_json, scan_result_digest
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (*key.sql_values(), rendered, digest),
            )
            row = conn.execute(
                """SELECT scan_result_json, scan_result_digest
                   FROM scan_reconstruction_cache
                   WHERE security_id=? AND date=? AND detector=?
                     AND detector_version=? AND input_revision=?""",
                key.sql_values(),
            ).fetchone()
            if row is None:
                raise BacktestIntegrityError("detector cache write was not visible")
            stored = self._validated_stored_fragment(key, str(row[0]), str(row[1]))
            if str(row[0]) != rendered or str(row[1]) != digest:
                raise BacktestIntegrityError(
                    "immutable detector cache key has conflicting content"
                )
            return stored

    def detector_fragment(
        self, key: DetectorCacheKey
    ) -> DetectorFragmentEnvelopeV1 | None:
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT scan_result_json, scan_result_digest
                   FROM scan_reconstruction_cache
                   WHERE security_id=? AND date=? AND detector=?
                     AND detector_version=? AND input_revision=?""",
                key.sql_values(),
            ).fetchone()
        if row is None:
            return None
        return self._validated_stored_fragment(key, str(row[0]), str(row[1]))

    def detector_cache_count(self) -> int:
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM scan_reconstruction_cache"
            ).fetchone()
        return 0 if row is None else int(row[0])

    @staticmethod
    def _verify_fragment_key(
        key: DetectorCacheKey, envelope: DetectorFragmentEnvelopeV1
    ) -> None:
        if (
            envelope.security_id,
            envelope.date,
            envelope.detector,
            envelope.detector_version,
            envelope.input_revision,
        ) != (
            key.security_id,
            key.date,
            key.detector,
            key.detector_version,
            key.input_revision,
        ):
            raise BacktestIntegrityError(
                "detector fragment envelope does not match cache key"
            )

    @classmethod
    def _validated_stored_fragment(
        cls, key: DetectorCacheKey, rendered: str, digest: str
    ) -> DetectorFragmentEnvelopeV1:
        raw = rendered.encode("utf-8")
        if sha256(raw).hexdigest() != digest:
            raise BacktestIntegrityError("detector cache digest is invalid")
        try:
            envelope = DetectorFragmentEnvelopeV1.from_canonical_json(raw)
        except HistoricalScanContractError as exc:
            raise BacktestIntegrityError("stored detector fragment is invalid") from exc
        cls._verify_fragment_key(key, envelope)
        return envelope

    def commit_roster_capture(self, commit: RosterCaptureCommit) -> str:
        """Atomically compare-and-insert a complete roster capture."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT roster_digest FROM reconstruction_roster_lineages WHERE lineage_id=?",
                (commit.lineage_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) == commit.roster_digest:
                    return commit.roster_digest
                raise sqlite3.IntegrityError(
                    "lineage is already bound to a different roster"
                )

            roster_preexisting = (
                conn.execute(
                    "SELECT 1 FROM reconstruction_rosters WHERE roster_digest=?",
                    (commit.roster_digest,),
                ).fetchone()
                is not None
            )
            alias_preexisting = (
                conn.execute(
                    "SELECT 1 FROM security_alias_manifests WHERE alias_revision=?",
                    (commit.alias_revision,),
                ).fetchone()
                is not None
            )
            self._insert_or_verify(
                conn,
                "security_identity_registry_revisions",
                "revision_digest",
                commit.identity_registry_revision,
                "canonical_manifest_json",
                commit.identity_registry_json,
                """INSERT INTO security_identity_registry_revisions
                   (revision_digest, canonical_manifest_json, evidence_digest, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    commit.identity_registry_revision,
                    commit.identity_registry_json,
                    commit.identity_evidence_digest,
                    commit.captured_at,
                ),
            )
            for security_id, mic, symbol, evidence_digest in commit.identities:
                conn.execute(
                    """INSERT OR IGNORE INTO security_identities
                       (security_id, mic, provider_symbol, evidence_digest,
                        identity_registry_revision, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        security_id,
                        mic,
                        symbol,
                        evidence_digest,
                        commit.identity_registry_revision,
                        commit.captured_at,
                    ),
                )
                row = conn.execute(
                    """SELECT security_id, evidence_digest FROM security_identities
                       WHERE mic=? AND provider_symbol=?""",
                    (mic, symbol),
                ).fetchone()
                if row != (security_id, evidence_digest):
                    raise sqlite3.IntegrityError(
                        "canonical identity conflicts with existing security"
                    )

            self._insert_or_verify(
                conn,
                "security_alias_manifests",
                "alias_revision",
                commit.alias_revision,
                "canonical_manifest_json",
                commit.alias_manifest_json,
                """INSERT INTO security_alias_manifests
                   (alias_revision, canonical_manifest_json, evidence_digest, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    commit.alias_revision,
                    commit.alias_manifest_json,
                    commit.alias_evidence_digest,
                    commit.captured_at,
                ),
            )
            if not alias_preexisting:
                for alias in commit.aliases:
                    conn.execute(
                        """INSERT INTO security_alias_entries
                           (alias_revision, security_id, provider, mic, observed_symbol,
                            effective_from, effective_to, evidence_source, evidence_digest,
                            provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (commit.alias_revision, *alias),
                    )

            self._insert_or_verify(
                conn,
                "reconstruction_rosters",
                "roster_digest",
                commit.roster_digest,
                "canonical_manifest_json",
                commit.roster_manifest_json,
                """INSERT INTO reconstruction_rosters
                   (roster_digest, policy_version, canonical_manifest_json,
                    identity_registry_revision, alias_revision, captured_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    commit.roster_digest,
                    commit.policy_version,
                    commit.roster_manifest_json,
                    commit.identity_registry_revision,
                    commit.alias_revision,
                    commit.captured_at,
                ),
            )
            if not roster_preexisting:
                for source_order, source in enumerate(commit.sources):
                    conn.execute(
                        """INSERT INTO reconstruction_roster_sources
                           (roster_digest, source_name, payload_digest,
                            original_payload_json, retrieved_at, source_order)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (commit.roster_digest, *source, source_order),
                    )
                for member in commit.members:
                    conn.execute(
                        """INSERT INTO reconstruction_roster_members
                           (roster_digest, security_id, mic, provider_symbol, currency,
                            source_memberships_json, identity_evidence_json,
                            evidence_digest)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (commit.roster_digest, *member),
                    )
            conn.execute(
                """INSERT INTO reconstruction_roster_lineages
                   (lineage_id, roster_digest, bound_at) VALUES (?, ?, ?)""",
                (commit.lineage_id, commit.roster_digest, commit.captured_at),
            )
        return commit.roster_digest

    @staticmethod
    def _insert_or_verify(
        conn: sqlite3.Connection,
        table: str,
        key_column: str,
        key: str,
        content_column: str,
        content: str,
        insert_sql: str,
        insert_values: tuple[object, ...],
    ) -> None:
        row = conn.execute(
            f"SELECT {content_column} FROM {table} WHERE {key_column}=?", (key,)
        ).fetchone()
        if row is None:
            conn.execute(insert_sql, insert_values)
        elif str(row[0]) != content:
            raise sqlite3.IntegrityError(f"{table} digest collision")
