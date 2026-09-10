"""Immutable persistence for provider-native historical market evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import sqlite3
from typing import Mapping, Sequence
import zlib

from app.repositories.db import Connect, evidence_connect, session
from app.services.backtest.canonical_manifest import (
    canonical_json,
    canonical_json_digest,
    manifest_digest,
)
from app.services.backtest.historical_price_evidence import (
    EVIDENCE_CONTRACT_VERSION,
    HistoricalEvidencePayload,
)

_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS historical_price_revisions (
    data_revision TEXT PRIMARY KEY,
    security_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider = 'yfinance'),
    provider_version TEXT NOT NULL,
    request_contract_version TEXT NOT NULL,
    requested_symbol TEXT NOT NULL,
    observed_symbol TEXT NOT NULL,
    alias_revision TEXT,
    currency TEXT NOT NULL,
    quote_unit TEXT NOT NULL,
    quote_unit_scale TEXT NOT NULL,
    exchange_timezone TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    request_contract_json TEXT NOT NULL,
    response_metadata_digest TEXT NOT NULL,
    canonical_manifest_json TEXT NOT NULL,
    observation_count INTEGER NOT NULL CHECK(observation_count > 0),
    action_count INTEGER NOT NULL CHECK(action_count >= 0),
    first_acquired_at TEXT NOT NULL,
    CHECK(start_date < end_date)
);
CREATE INDEX IF NOT EXISTS idx_historical_revision_interval
ON historical_price_revisions(
    security_id, provider, start_date, end_date, request_contract_version
);
-- Without this, dated_close's requested_symbol filter has nothing to seek
-- on, and the query planner chooses to scan the whole (many-million-row)
-- historical_price_observations table instead of the much smaller
-- revisions table -- turning one lookup into tens of seconds (#480/#481).
CREATE INDEX IF NOT EXISTS idx_historical_revisions_requested_symbol
ON historical_price_revisions(requested_symbol);
CREATE TABLE IF NOT EXISTS historical_price_observations (
    data_revision TEXT NOT NULL REFERENCES historical_price_revisions(data_revision),
    session_date TEXT NOT NULL,
    open_hex TEXT NOT NULL,
    high_hex TEXT NOT NULL,
    low_hex TEXT NOT NULL,
    close_hex TEXT NOT NULL,
    adj_close_hex TEXT,
    volume_hex TEXT NOT NULL,
    dividends_hex TEXT NOT NULL,
    stock_splits_hex TEXT NOT NULL,
    PRIMARY KEY(data_revision, session_date)
);
CREATE TABLE IF NOT EXISTS historical_corporate_actions (
    data_revision TEXT NOT NULL REFERENCES historical_price_revisions(data_revision),
    session_date TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('dividend', 'split')),
    value_hex TEXT NOT NULL,
    PRIMARY KEY(data_revision, session_date, action_type)
);
CREATE TABLE IF NOT EXISTS historical_price_acquisitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_revision TEXT NOT NULL REFERENCES historical_price_revisions(data_revision),
    acquired_at TEXT NOT NULL,
    response_metadata_digest TEXT NOT NULL,
    UNIQUE(data_revision, acquired_at, response_metadata_digest)
);
CREATE TABLE IF NOT EXISTS historical_evidence_references (
    consumer_type TEXT NOT NULL CHECK(consumer_type IN ('snapshot', 'backtest')),
    consumer_id TEXT NOT NULL,
    data_revision TEXT NOT NULL REFERENCES historical_price_revisions(data_revision),
    created_at TEXT NOT NULL,
    PRIMARY KEY(consumer_type, consumer_id, data_revision)
);

CREATE TABLE IF NOT EXISTS historical_price_v2_revisions (
    revision_id INTEGER PRIMARY KEY,
    data_revision TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL,
    response_metadata_digest TEXT NOT NULL,
    observation_count INTEGER NOT NULL CHECK(observation_count > 0),
    action_count INTEGER NOT NULL CHECK(action_count >= 0),
    first_acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS historical_price_v2_chunks (
    chunk_digest TEXT PRIMARY KEY,
    codec TEXT NOT NULL CHECK(codec = 'zlib'),
    format_version INTEGER NOT NULL CHECK(format_version = 1),
    compressed_payload BLOB NOT NULL,
    uncompressed_bytes INTEGER NOT NULL CHECK(uncompressed_bytes > 0)
);
CREATE TABLE IF NOT EXISTS historical_price_v2_revision_chunks (
    revision_id INTEGER NOT NULL REFERENCES historical_price_v2_revisions(revision_id),
    chunk_order INTEGER NOT NULL CHECK(chunk_order >= 0),
    chunk_kind TEXT NOT NULL CHECK(chunk_kind IN ('rows', 'actions')),
    chunk_year INTEGER NOT NULL,
    chunk_digest TEXT NOT NULL REFERENCES historical_price_v2_chunks(chunk_digest),
    PRIMARY KEY(revision_id, chunk_order),
    UNIQUE(revision_id, chunk_kind, chunk_year)
);
CREATE TABLE IF NOT EXISTS historical_price_storage_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    active_format TEXT NOT NULL CHECK(active_format IN ('v1', 'v2')),
    activated_at TEXT,
    activation_review TEXT
);
INSERT OR IGNORE INTO historical_price_storage_state
    (singleton_id, active_format, activated_at)
    VALUES (1, 'v1', NULL);
CREATE TABLE IF NOT EXISTS historical_price_v2_migration_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    source_fingerprint TEXT NOT NULL,
    source_revision_count INTEGER NOT NULL,
    last_data_revision TEXT,
    migrated_revision_count INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT
);

CREATE TRIGGER IF NOT EXISTS historical_v2_revision_immutable_update BEFORE UPDATE ON historical_price_v2_revisions BEGIN SELECT RAISE(ABORT, 'historical v2 revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_v2_revision_immutable_delete BEFORE DELETE ON historical_price_v2_revisions BEGIN SELECT RAISE(ABORT, 'historical v2 revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_v2_chunk_immutable_update BEFORE UPDATE ON historical_price_v2_chunks BEGIN SELECT RAISE(ABORT, 'historical v2 chunk is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_v2_chunk_immutable_delete BEFORE DELETE ON historical_price_v2_chunks BEGIN SELECT RAISE(ABORT, 'historical v2 chunk is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_v2_mapping_immutable_update BEFORE UPDATE ON historical_price_v2_revision_chunks BEGIN SELECT RAISE(ABORT, 'historical v2 mapping is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_v2_mapping_immutable_delete BEFORE DELETE ON historical_price_v2_revision_chunks BEGIN SELECT RAISE(ABORT, 'historical v2 mapping is immutable'); END;

CREATE TRIGGER IF NOT EXISTS historical_revision_immutable_update BEFORE UPDATE ON historical_price_revisions BEGIN SELECT RAISE(ABORT, 'historical revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_revision_immutable_delete BEFORE DELETE ON historical_price_revisions BEGIN SELECT RAISE(ABORT, 'historical revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_observation_immutable_update BEFORE UPDATE ON historical_price_observations BEGIN SELECT RAISE(ABORT, 'historical observation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_observation_immutable_delete BEFORE DELETE ON historical_price_observations BEGIN SELECT RAISE(ABORT, 'historical observation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_action_immutable_update BEFORE UPDATE ON historical_corporate_actions BEGIN SELECT RAISE(ABORT, 'historical action is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_action_immutable_delete BEFORE DELETE ON historical_corporate_actions BEGIN SELECT RAISE(ABORT, 'historical action is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_acquisition_immutable_update BEFORE UPDATE ON historical_price_acquisitions BEGIN SELECT RAISE(ABORT, 'historical acquisition is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_acquisition_immutable_delete BEFORE DELETE ON historical_price_acquisitions BEGIN SELECT RAISE(ABORT, 'historical acquisition is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_reference_immutable_update BEFORE UPDATE ON historical_evidence_references BEGIN SELECT RAISE(ABORT, 'historical reference is immutable'); END;
CREATE TRIGGER IF NOT EXISTS historical_reference_immutable_delete BEFORE DELETE ON historical_evidence_references BEGIN SELECT RAISE(ABORT, 'historical reference is immutable'); END;

CREATE TABLE IF NOT EXISTS price_evidence_unavailable_attempts (
    security_id      TEXT PRIMARY KEY,
    requested_symbol TEXT NOT NULL,
    reason           TEXT NOT NULL,
    attempted_at     TEXT NOT NULL,
    contract_version TEXT NOT NULL DEFAULT ''
);
"""

#: Databases written before #516 have the table without its version column;
#: their rows then default to ``''``, which no current contract version can
#: equal -- exactly the "recorded under older rules, ignore it" outcome.
_ADD_CONTRACT_VERSION = (
    "ALTER TABLE price_evidence_unavailable_attempts "
    "ADD COLUMN contract_version TEXT NOT NULL DEFAULT ''"
)


#: SQLite's wording for the two ways an evidence-free cache presents itself:
#: the file (or its directory) does not exist, or it exists without schema.
_ABSENT_CACHE_ERRORS = ("unable to open database file", "no such table")


def _is_absent_cache(error: sqlite3.OperationalError) -> bool:
    """Return True when the error means "no cache", not "cache unreadable"."""
    message = str(error).lower()
    return any(reason in message for reason in _ABSENT_CACHE_ERRORS)


def _hex_to_float(value: str) -> float:
    """Parse a stored C99 hex float, returning NaN for an unparseable one."""
    try:
        return float.fromhex(value)
    except (TypeError, ValueError):
        return float("nan")


class EvidenceMissingError(LookupError):
    code = "evidence_missing"


class HistoricalEvidenceIntegrityError(RuntimeError):
    code = "integrity_error"


@dataclass(frozen=True)
class StoredHistoricalEvidence:
    data_revision: str
    security_id: str
    provider: str
    provider_version: str
    request_contract_version: str
    requested_symbol: str
    observed_symbol: str
    alias_revision: str | None
    currency: str
    quote_unit: str
    quote_unit_scale: str
    exchange_timezone: str
    start: str
    end: str
    request_contract: Mapping[str, object]
    response_metadata_digest: str
    canonical_manifest_json: str
    rows: tuple[Mapping[str, object], ...]
    actions: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class DatedClose:
    """One stored close for one symbol on one exact session date.

    ``close`` is the as-traded close (``auto_adjust=false``), expressed in
    ``quote_unit``; multiply by ``quote_unit_scale`` to reach major units of
    ``currency``. It can be non-finite: yfinance is fetched with ``keepna``,
    so a NaN close is stored verbatim and callers must treat it as no
    evidence at all.
    """

    security_id: str
    data_revision: str
    currency: str
    quote_unit: str
    quote_unit_scale: str
    close: float


@dataclass(frozen=True)
class PriceEvidenceUnavailableAttempt:
    """A durable failed backfill attempt for one ``portfolio:``-namespaced
    security -- never retried once recorded (mirrors
    ``FxUnavailableAttempt``, but keyed by ``security_id`` alone since this
    is a single bulk range fetch, not a date-keyed lookup)."""

    security_id: str
    requested_symbol: str
    reason: str


@dataclass(frozen=True)
class HistoricalEvidenceMigrationProgress:
    source_revision_count: int
    migrated_revision_count: int
    completed: bool
    source_database_bytes: int
    available_bytes: int
    required_reserve_bytes: int


class HistoricalPriceRepository:
    """Own the append-only historical price database and exact-reference reads."""

    _v2_chunk_max_bytes = 64 * 1024 * 1024

    def __init__(self, connect: Connect) -> None:
        self._connect = evidence_connect(connect)

    def ensure_schema(self) -> None:
        with session(self._connect) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            try:
                conn.execute(_ADD_CONTRACT_VERSION)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
            try:
                conn.execute(
                    "ALTER TABLE historical_price_storage_state ADD COLUMN activation_review TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

    def migrate_v1_to_v2(
        self, *, max_revisions: int | None = None, available_bytes: int | None = None
    ) -> HistoricalEvidenceMigrationProgress:
        """Migrate a bounded offline v1 batch, checkpointing every revision."""
        if max_revisions is not None and max_revisions < 1:
            raise HistoricalEvidenceIntegrityError("migration batch size must be positive")
        self.ensure_schema()
        with session(self._connect) as conn:
            fingerprint, count = self._v1_source_identity(conn)
            path = self._database_path(conn)
            source_database_bytes = path.stat().st_size
            reserve = max(source_database_bytes // 4, 1_048_576)
            free = (
                shutil.disk_usage(path.parent).free
                if available_bytes is None
                else available_bytes
            )
            if free < reserve:
                raise HistoricalEvidenceIntegrityError(
                    "insufficient disk space for v2 migration"
                )
            state = conn.execute(
                """SELECT source_fingerprint, source_revision_count, last_data_revision,
                          migrated_revision_count, completed_at
                   FROM historical_price_v2_migration_state WHERE singleton_id=1"""
            ).fetchone()
            if state is None:
                conn.execute(
                    """INSERT INTO historical_price_v2_migration_state
                       (singleton_id, source_fingerprint, source_revision_count)
                       VALUES (1, ?, ?)""",
                    (fingerprint, count),
                )
                last, migrated, completed = None, 0, None
            else:
                if str(state[0]) != fingerprint or int(state[1]) != count:
                    raise HistoricalEvidenceIntegrityError(
                        "v1 migration source changed"
                    )
                last, migrated, completed = state[2], int(state[3]), state[4]
            if completed is not None:
                return HistoricalEvidenceMigrationProgress(
                    count, migrated, True, source_database_bytes, free, reserve
                )
            rows = conn.execute(
                """SELECT data_revision FROM historical_price_revisions
                   WHERE data_revision>? ORDER BY data_revision LIMIT ?""",
                (
                    "" if last is None else str(last),
                    -1 if max_revisions is None else max_revisions,
                ),
            ).fetchall()
        for row in rows:
            revision = str(row[0])
            with session(self._connect) as conn:
                evidence = self._load_on_connection(conn, revision)
                acquired = conn.execute(
                    """SELECT first_acquired_at, response_metadata_digest
                       FROM historical_price_revisions WHERE data_revision=?""",
                    (revision,),
                ).fetchone()
                assert acquired is not None
                payload = HistoricalEvidencePayload(
                    security_id=evidence.security_id,
                    alias_revision=evidence.alias_revision,
                    provider=evidence.provider,
                    provider_version=evidence.provider_version,
                    request_contract_version=evidence.request_contract_version,
                    requested_symbol=evidence.requested_symbol,
                    observed_symbol=evidence.observed_symbol,
                    currency=evidence.currency,
                    quote_unit=evidence.quote_unit,
                    quote_unit_scale=evidence.quote_unit_scale,
                    exchange_timezone=evidence.exchange_timezone,
                    start=evidence.start,
                    end=evidence.end,
                    request_contract=evidence.request_contract,
                    rows=evidence.rows,
                    actions=evidence.actions,
                    response_metadata_digest=str(acquired[1]),
                    data_revision=revision,
                    canonical_manifest_json=evidence.canonical_manifest_json,
                    acquired_at=str(acquired[0]),
                )
            self.commit_v2(payload)
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                current, current_count = self._v1_source_identity(conn)
                if current != fingerprint or current_count != count:
                    raise HistoricalEvidenceIntegrityError(
                        "v1 migration source changed"
                    )
                conn.execute(
                    """UPDATE historical_price_v2_migration_state
                       SET last_data_revision=?, migrated_revision_count=migrated_revision_count+1
                       WHERE singleton_id=1 AND source_fingerprint=?""",
                    (revision, fingerprint),
                )
                migrated += 1
        with session(self._connect) as conn:
            current, current_count = self._v1_source_identity(conn)
            if current != fingerprint or current_count != count:
                raise HistoricalEvidenceIntegrityError("v1 migration source changed")
            done = migrated == count
            if done:
                conn.execute(
                    "UPDATE historical_price_v2_migration_state SET completed_at=? WHERE singleton_id=1",
                    (datetime.now(timezone.utc).isoformat(),),
                )
        return HistoricalEvidenceMigrationProgress(
            count, migrated, done, source_database_bytes, free, reserve
        )

    def activate_v2(self, *, review_reference: str) -> None:
        """Atomically activate v2 reads after a recorded capacity review."""
        if not review_reference.strip():
            raise HistoricalEvidenceIntegrityError("v2 activation review is required")
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                "SELECT completed_at FROM historical_price_v2_migration_state WHERE singleton_id=1"
            ).fetchone()
            if state is None or state[0] is None:
                raise HistoricalEvidenceIntegrityError("v2 migration is incomplete")
            fingerprint, count = self._v1_source_identity(conn)
            migration = conn.execute(
                """SELECT source_fingerprint, source_revision_count, migrated_revision_count
                   FROM historical_price_v2_migration_state WHERE singleton_id=1"""
            ).fetchone()
            if migration is None or (
                str(migration[0]),
                int(migration[1]),
                int(migration[2]),
            ) != (fingerprint, count, count):
                raise HistoricalEvidenceIntegrityError("v1 migration source changed")
            conn.execute(
                """UPDATE historical_price_storage_state
                   SET active_format='v2', activated_at=?, activation_review=?
                   WHERE singleton_id=1""",
                (datetime.now(timezone.utc).isoformat(), review_reference),
            )

    def rollback_v2_activation(self) -> None:
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE historical_price_storage_state
                   SET active_format='v1', activated_at=NULL, activation_review=NULL
                   WHERE singleton_id=1"""
            )

    @staticmethod
    def _database_path(conn: sqlite3.Connection) -> Path:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None or not row[2]:
            raise HistoricalEvidenceIntegrityError(
                "migration database path is unavailable"
            )
        return Path(str(row[2]))

    @staticmethod
    def _v1_source_identity(conn: sqlite3.Connection) -> tuple[str, int]:
        revisions = [
            str(row[0])
            for row in conn.execute(
                "SELECT data_revision FROM historical_price_revisions ORDER BY data_revision"
            )
        ]
        return sha256("\n".join(revisions).encode()).hexdigest(), len(revisions)

    def commit(self, payload: HistoricalEvidencePayload) -> str:
        try:
            self._validate_payload(payload)
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._commit_v1_on_connection(conn, payload)
                active = conn.execute(
                    "SELECT active_format FROM historical_price_storage_state WHERE singleton_id=1"
                ).fetchone()
                if active is not None and str(active[0]) == "v2":
                    self._commit_v2_on_connection(conn, payload)
            return payload.data_revision
        except HistoricalEvidenceIntegrityError:
            raise
        except sqlite3.IntegrityError as exc:
            raise HistoricalEvidenceIntegrityError(
                "historical evidence transaction failed"
            ) from exc

    def _commit(self, payload: HistoricalEvidencePayload) -> str:
        """Append v1 evidence for legacy callers and migration fixtures."""
        self._validate_payload(payload)
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._commit_v1_on_connection(conn, payload)
        return payload.data_revision

    @staticmethod
    def _validate_payload(payload: HistoricalEvidencePayload) -> None:
        if payload.security_id is None:
            raise HistoricalEvidenceIntegrityError("resolved security_id is required")
        try:
            canonical = json.loads(payload.canonical_manifest_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HistoricalEvidenceIntegrityError(
                "invalid canonical manifest"
            ) from exc
        if manifest_digest(canonical) != payload.data_revision:
            raise HistoricalEvidenceIntegrityError("revision digest mismatch")
        if canonical.get("rows") != list(payload.rows) or canonical.get(
            "actions"
        ) != list(payload.actions):
            raise HistoricalEvidenceIntegrityError("manifest payload mismatch")

    def _commit_v1_on_connection(
        self, conn: sqlite3.Connection, payload: HistoricalEvidencePayload
    ) -> None:
        existing = conn.execute(
            "SELECT canonical_manifest_json FROM historical_price_revisions "
            "WHERE data_revision=?",
            (payload.data_revision,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != payload.canonical_manifest_json:
                raise HistoricalEvidenceIntegrityError("revision digest collision")
            self._verify_on_connection(conn, payload.data_revision)
        else:
            conn.execute(
                """INSERT INTO historical_price_revisions (
                        data_revision, security_id, provider, provider_version,
                        request_contract_version, requested_symbol, observed_symbol,
                        alias_revision, currency, quote_unit, quote_unit_scale,
                        exchange_timezone, start_date, end_date, request_contract_json,
                        response_metadata_digest, canonical_manifest_json,
                        observation_count, action_count, first_acquired_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload.data_revision,
                    payload.security_id,
                    payload.provider,
                    payload.provider_version,
                    payload.request_contract_version,
                    payload.requested_symbol,
                    payload.observed_symbol,
                    payload.alias_revision,
                    payload.currency,
                    payload.quote_unit,
                    payload.quote_unit_scale,
                    payload.exchange_timezone,
                    payload.start,
                    payload.end,
                    json.dumps(
                        payload.request_contract,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    payload.response_metadata_digest,
                    payload.canonical_manifest_json,
                    len(payload.rows),
                    len(payload.actions),
                    payload.acquired_at,
                ),
            )
            for row in payload.rows:
                conn.execute(
                    """INSERT INTO historical_price_observations (
                            data_revision, session_date, open_hex, high_hex, low_hex,
                            close_hex, adj_close_hex, volume_hex, dividends_hex,
                            stock_splits_hex
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload.data_revision,
                        row["session"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["adj_close"],
                        row["volume"],
                        row["dividends"],
                        row["stock_splits"],
                    ),
                )
            for action in payload.actions:
                conn.execute(
                    """INSERT INTO historical_corporate_actions
                           (data_revision, session_date, action_type, value_hex)
                           VALUES (?, ?, ?, ?)""",
                    (
                        payload.data_revision,
                        action["session"],
                        action["action_type"],
                        action["value"],
                    ),
                )
            self._verify_on_connection(conn, payload.data_revision)
        conn.execute(
            """INSERT OR IGNORE INTO historical_price_acquisitions
                   (data_revision, acquired_at, response_metadata_digest)
                   VALUES (?, ?, ?)""",
            (
                payload.data_revision,
                payload.acquired_at,
                payload.response_metadata_digest,
            ),
        )

    def commit_v2(self, payload: HistoricalEvidencePayload) -> str:
        """Append one independently verifiable v2 evidence representation.

        Kept as an explicit entrypoint so migration can add v2 records before
        activation. Normal writes add both formats once v2 is active.
        """
        self._validate_payload(payload)
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._commit_v2_on_connection(conn, payload)
        return payload.data_revision

    def _commit_v2_on_connection(
        self, conn: sqlite3.Connection, payload: HistoricalEvidencePayload
    ) -> None:
        canonical = json.loads(payload.canonical_manifest_json)
        metadata = dict(canonical)
        metadata.pop("rows", None)
        metadata.pop("actions", None)
        chunks = self._v2_chunks(payload.rows, payload.actions)
        existing = conn.execute(
            "SELECT revision_id FROM historical_price_v2_revisions WHERE data_revision=?",
            (payload.data_revision,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO historical_price_v2_revisions
                       (data_revision, metadata_json, response_metadata_digest,
                        observation_count, action_count, first_acquired_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    payload.data_revision,
                    canonical_json(metadata),
                    payload.response_metadata_digest,
                    len(payload.rows),
                    len(payload.actions),
                    payload.acquired_at,
                ),
            )
            revision_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            for order, (kind, year, encoded, digest) in enumerate(chunks):
                conn.execute(
                    """INSERT OR IGNORE INTO historical_price_v2_chunks
                           (chunk_digest, codec, format_version, compressed_payload,
                            uncompressed_bytes) VALUES (?, 'zlib', 1, ?, ?)""",
                    (digest, zlib.compress(encoded), len(encoded)),
                )
                conn.execute(
                    """INSERT INTO historical_price_v2_revision_chunks
                           (revision_id, chunk_order, chunk_kind, chunk_year, chunk_digest)
                           VALUES (?, ?, ?, ?, ?)""",
                    (revision_id, order, kind, year, digest),
                )
        self._verify_v2_on_connection(conn, payload.data_revision)

    @staticmethod
    def _v2_chunks(
        rows: Sequence[Mapping[str, object]], actions: Sequence[Mapping[str, object]]
    ) -> tuple[tuple[str, int, bytes, str], ...]:
        grouped: dict[tuple[str, int], list[Mapping[str, object]]] = {}
        for kind, items in (("rows", rows), ("actions", actions)):
            for item in items:
                try:
                    year = int(str(item["session"])[:4])
                except (KeyError, TypeError, ValueError) as exc:
                    raise HistoricalEvidenceIntegrityError(
                        "v2 chunk session is invalid"
                    ) from exc
                grouped.setdefault((kind, year), []).append(item)
        chunks = []
        for (kind, year), items in sorted(grouped.items()):
            encoded = canonical_json(
                {"kind": kind, "year": year, "items": items}
            ).encode()
            if len(encoded) > HistoricalPriceRepository._v2_chunk_max_bytes:
                raise HistoricalEvidenceIntegrityError("v2 chunk exceeds size limit")
            chunks.append((kind, year, encoded, sha256(encoded).hexdigest()))
        return tuple(chunks)

    def get(self, data_revision: str) -> StoredHistoricalEvidence:
        with session(self._connect) as conn:
            active = conn.execute(
                "SELECT active_format FROM historical_price_storage_state WHERE singleton_id=1"
            ).fetchone()
            if active is not None and str(active[0]) == "v2":
                return self._verify_v2_on_connection(conn, data_revision)
            try:
                return self._load_on_connection(conn, data_revision)
            except EvidenceMissingError:
                return self._verify_v2_on_connection(conn, data_revision)

    def get_v2(self, data_revision: str) -> StoredHistoricalEvidence:
        """Read and fully verify one opt-in v2 evidence revision."""
        with session(self._connect) as conn:
            return self._verify_v2_on_connection(conn, data_revision)

    def get_exact(
        self, *, security_id: str, start: str, end: str, data_revision: str
    ) -> StoredHistoricalEvidence:
        evidence = self.get(data_revision)
        if (
            evidence.security_id != security_id
            or evidence.start != start
            or evidence.end != end
        ):
            raise EvidenceMissingError("exact historical evidence is missing")
        return evidence

    def find_request(
        self,
        *,
        security_id: str,
        requested_symbol: str,
        alias_revision: str | None,
        start: str,
        end: str,
        request_contract_version: str,
        observation_policy: str | None = None,
    ) -> StoredHistoricalEvidence | None:
        """Return the earliest immutable revision for one exact request identity."""
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT data_revision FROM historical_price_revisions
                   WHERE security_id=? AND requested_symbol=?
                     AND alias_revision IS ? AND start_date=? AND end_date=?
                     AND request_contract_version=?
                     AND json_extract(request_contract_json, '$.observation_policy') IS ?
                   ORDER BY first_acquired_at, data_revision LIMIT 1""",
                (
                    security_id,
                    requested_symbol,
                    alias_revision,
                    start,
                    end,
                    request_contract_version,
                    observation_policy,
                ),
            ).fetchone()
            if row is None:
                return None
            return self._verify_on_connection(conn, str(row[0]))

    def find_compatible_request(
        self,
        *,
        security_id: str,
        requested_symbol: str,
        start: str,
        end: str,
        request_contract_version: str,
        observation_policy: str | None = None,
    ) -> StoredHistoricalEvidence | None:
        """Return verified evidence whose only identity drift may be aliases."""
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT data_revision FROM historical_price_revisions
                   WHERE security_id=? AND requested_symbol=?
                     AND start_date=? AND end_date=?
                     AND request_contract_version=?
                     AND json_extract(request_contract_json, '$.observation_policy') IS ?
                   ORDER BY first_acquired_at, data_revision LIMIT 1""",
                (
                    security_id,
                    requested_symbol,
                    start,
                    end,
                    request_contract_version,
                    observation_policy,
                ),
            ).fetchone()
            if row is None:
                return None
            return self._verify_on_connection(conn, str(row[0]))

    def dated_close(
        self, symbols: Sequence[str], session_date: str
    ) -> DatedClose | None:
        """Return the stored close for one of ``symbols`` on ``session_date``.

        A narrow, read-only lookup for callers that hold a portfolio ticker
        rather than a backtest ``security_id``: it matches on
        ``requested_symbol`` (pass every alias spelling of the ticker) and
        loads exactly one observation, unlike ``find_compatible_request``,
        which needs a ``security_id`` and re-verifies a whole revision
        manifest. Overlapping revisions are resolved deterministically --
        newest ``first_acquired_at``, ties broken by ``data_revision``.

        Returns None when the cache holds no observation for that exact date
        (never a nearby session) or when the cache database has no schema yet.
        A database that exists but cannot be read -- locked, corrupt, an I/O
        error -- raises rather than reporting "no evidence": a caller acting
        on that answer would erase real valuations on a transient fault.
        """
        if not symbols:
            return None
        placeholders = ",".join("?" for _ in symbols)
        query = (
            "SELECT r.security_id, r.data_revision, r.currency, r.quote_unit, "
            "r.quote_unit_scale, o.close_hex "
            "FROM historical_price_revisions r "
            "JOIN historical_price_observations o "
            "ON o.data_revision = r.data_revision "
            f"WHERE r.requested_symbol IN ({placeholders}) AND o.session_date = ? "
            "ORDER BY r.first_acquired_at DESC, r.data_revision LIMIT 1"
        )
        try:
            with session(self._connect) as conn:
                row = conn.execute(query, [*symbols, session_date]).fetchone()
        except sqlite3.OperationalError as exc:
            # A cache that was never created is simply evidence-free: it
            # cannot be opened (no directory/file) or has no tables yet.
            # Anything else -- locked, disk I/O error -- must surface.
            if not _is_absent_cache(exc):
                raise
            return None
        if row is None:
            return None
        return DatedClose(
            security_id=str(row[0]),
            data_revision=str(row[1]),
            currency=str(row[2]),
            quote_unit=str(row[3]),
            quote_unit_scale=str(row[4]),
            close=_hex_to_float(str(row[5])),
        )

    def split_factor_since(
        self, symbols: Sequence[str], session_date: str
    ) -> float | None:
        """Return the cumulative split factor applied after ``session_date``.

        A stored close is what the provider published *after* adjusting for
        every split up to the day it was fetched, so comparing it with a
        price actually paid before one is comparing two different share
        definitions (#555): TSLA's 2021 fills sit near $690 while its stored
        2021 closes sit near $230, because of a 3:1 split in 2022.
        Multiplying the close by this factor restores it to the shares that
        existed on the day.

        Splits are read across *every* revision of these symbols, not the
        one :meth:`dated_close` happens to pick. A revision's window and its
        adjustment baseline are different things -- the narrow
        ``2020-12-01..2021-05-12`` revision this account holds for TSLA was
        fetched in 2026 and is adjusted for a 2022 split its own window
        cannot contain -- so asking only the covering revision reports no
        split at all. One ratio per split date (the newest revision's, where
        they overlap), so a split recorded by several revisions is counted
        once.

        ``None`` when no revision exists for these symbols, or when a stored
        ratio is unusable; ``1.0`` -- an honest "no adjustment" -- when
        revisions exist and record no later split.
        """
        if not symbols:
            return None
        placeholders = ",".join("?" for _ in symbols)
        try:
            with session(self._connect) as conn:
                if (
                    conn.execute(
                        "SELECT 1 FROM historical_price_revisions "
                        f"WHERE requested_symbol IN ({placeholders}) LIMIT 1",
                        list(symbols),
                    ).fetchone()
                    is None
                ):
                    return None
                rows = conn.execute(
                    "SELECT a.session_date, a.value_hex "
                    "FROM historical_price_revisions r "
                    "JOIN historical_corporate_actions a "
                    "ON a.data_revision = r.data_revision "
                    f"WHERE r.requested_symbol IN ({placeholders}) "
                    "AND a.action_type = 'split' AND a.session_date > ? "
                    "ORDER BY r.first_acquired_at DESC, r.data_revision",
                    [*symbols, session_date],
                ).fetchall()
        except sqlite3.OperationalError as exc:
            # Same contract as ``dated_close``: an absent cache is simply
            # evidence-free, anything else must surface.
            if not _is_absent_cache(exc):
                raise
            return None
        ratios: dict[str, float] = {}
        for split_date, value_hex in rows:
            ratios.setdefault(str(split_date), _hex_to_float(str(value_hex)))
        factor = 1.0
        for ratio in ratios.values():
            if not math.isfinite(ratio) or ratio <= 0:
                return None
            factor *= ratio
        return factor

    def covering_revision(
        self, *, security_id: str, requested_symbol: str, start: str, end: str
    ) -> str | None:
        """Return a ``data_revision`` whose stored interval contains
        ``[start, end)``, or None.

        Unlike ``find_request``/``find_compatible_request``, which match an
        *exact* interval for a known roster identity, this matches any
        revision whose interval *contains* the requested one -- repair's
        range only grows as new snapshots appear, so an earlier, wider
        fetch already covers a later, narrower request. Ties (more than one
        covering revision) resolve to the newest ``first_acquired_at``.
        """
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT data_revision FROM historical_price_revisions
                   WHERE security_id=? AND requested_symbol=?
                     AND start_date<=? AND end_date>=?
                   ORDER BY first_acquired_at DESC, data_revision LIMIT 1""",
                (security_id, requested_symbol, start, end),
            ).fetchone()
        return None if row is None else str(row[0])

    def get_unavailable_attempt(
        self,
        security_id: str,
        *,
        contract_version: str = EVIDENCE_CONTRACT_VERSION,
    ) -> PriceEvidenceUnavailableAttempt | None:
        """Return the permanent failure recorded for ``security_id``, or None.

        A row recorded under a *different* contract version is not a verdict
        on the current rules and is ignored (#516): without this, a refusal
        decided by the old GBP/USD-only currency gate would keep blocking the
        very securities that widening the gate was meant to admit -- and every
        future widening would be self-blocking in the same way.
        """
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT security_id, requested_symbol, reason "
                "FROM price_evidence_unavailable_attempts "
                "WHERE security_id=? AND contract_version=?",
                (security_id, contract_version),
            ).fetchone()
        return (
            None
            if row is None
            else PriceEvidenceUnavailableAttempt(
                security_id=str(row[0]),
                requested_symbol=str(row[1]),
                reason=str(row[2]),
            )
        )

    def record_unavailable_attempt(
        self,
        *,
        security_id: str,
        requested_symbol: str,
        reason: str,
        contract_version: str = EVIDENCE_CONTRACT_VERSION,
    ) -> None:
        """Persist a permanent backfill failure under the rules that decided it.

        ``REPLACE`` rather than ``IGNORE``: a fresh refusal supersedes one
        recorded under an older contract version, which the reader ignores
        anyway -- keeping the stale row would leave the ticker permanently
        re-fetched on every run.
        """
        attempted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with session(self._connect) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO price_evidence_unavailable_attempts "
                "(security_id, requested_symbol, reason, attempted_at, "
                "contract_version) VALUES (?, ?, ?, ?, ?)",
                (
                    security_id,
                    requested_symbol,
                    reason,
                    attempted_at,
                    contract_version,
                ),
            )

    def quoted_currencies(self, security_id_prefix: str) -> frozenset[str]:
        """Return every currency stored evidence under ``prefix`` is quoted in.

        The only way to know which FX pairs a portfolio actually needs (#516):
        a holding's currency is not derivable from its ticker, it is what the
        provider reported when its price evidence was committed. An absent
        cache is evidence-free, not an error -- same contract as
        :meth:`dated_close`.
        """
        try:
            with session(self._connect) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT currency FROM historical_price_revisions "
                    "WHERE security_id LIKE ?",
                    (f"{security_id_prefix}%",),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if not _is_absent_cache(exc):
                raise
            return frozenset()
        return frozenset(str(row[0]).strip().upper() for row in rows)

    def verify(self, data_revision: str) -> StoredHistoricalEvidence:
        with session(self._connect) as conn:
            return self._verify_on_connection(conn, data_revision)

    def pin(self, consumer_type: str, consumer_id: str, data_revision: str) -> None:
        if consumer_type not in {"snapshot", "backtest"}:
            raise ValueError("unsupported evidence consumer type")
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._verify_on_connection(conn, data_revision)
            row = conn.execute(
                "SELECT first_acquired_at FROM historical_price_revisions "
                "WHERE data_revision=?",
                (data_revision,),
            ).fetchone()
            assert row is not None
            conn.execute(
                """INSERT OR IGNORE INTO historical_evidence_references
                   (consumer_type, consumer_id, data_revision, created_at)
                   VALUES (?, ?, ?, ?)""",
                (consumer_type, consumer_id, data_revision, str(row[0])),
            )

    def acquisition_times(self, data_revision: str) -> tuple[str, ...]:
        with session(self._connect) as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM historical_price_revisions WHERE data_revision=?",
                    (data_revision,),
                ).fetchone()
                is None
            ):
                raise EvidenceMissingError("historical evidence is missing")
            rows = conn.execute(
                "SELECT acquired_at FROM historical_price_acquisitions "
                "WHERE data_revision=? ORDER BY acquired_at, id",
                (data_revision,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _verify_on_connection(
        self, conn: sqlite3.Connection, data_revision: str
    ) -> StoredHistoricalEvidence:
        evidence = self._load_on_connection(conn, data_revision)
        try:
            canonical = json.loads(evidence.canonical_manifest_json)
        except json.JSONDecodeError as exc:
            raise HistoricalEvidenceIntegrityError("invalid stored manifest") from exc
        if canonical_json_digest(evidence.canonical_manifest_json) != data_revision:
            raise HistoricalEvidenceIntegrityError("stored revision digest mismatch")
        if canonical.get("rows") != list(evidence.rows):
            raise HistoricalEvidenceIntegrityError("stored observation mismatch")
        if canonical.get("actions") != list(evidence.actions):
            raise HistoricalEvidenceIntegrityError("stored action mismatch")
        return evidence

    @staticmethod
    def _verify_v2_on_connection(
        conn: sqlite3.Connection, data_revision: str
    ) -> StoredHistoricalEvidence:
        revision = conn.execute(
            """SELECT revision_id, metadata_json, response_metadata_digest,
                      observation_count, action_count
               FROM historical_price_v2_revisions WHERE data_revision=?""",
            (data_revision,),
        ).fetchone()
        if revision is None:
            raise EvidenceMissingError("historical evidence is missing")
        try:
            metadata = json.loads(str(revision[1]))
        except json.JSONDecodeError as exc:
            raise HistoricalEvidenceIntegrityError("invalid v2 metadata") from exc
        rows: list[Mapping[str, object]] = []
        actions: list[Mapping[str, object]] = []
        mappings = conn.execute(
            """SELECT mapping.chunk_kind, mapping.chunk_year, chunk.chunk_digest,
                      chunk.codec, chunk.format_version, chunk.compressed_payload,
                      chunk.uncompressed_bytes
               FROM historical_price_v2_revision_chunks AS mapping
               JOIN historical_price_v2_chunks AS chunk
                 ON chunk.chunk_digest=mapping.chunk_digest
               WHERE mapping.revision_id=? ORDER BY mapping.chunk_order""",
            (int(revision[0]),),
        ).fetchall()
        for kind, year, digest, codec, version, compressed, size in mappings:
            if str(codec) != "zlib" or int(version) != 1:
                raise HistoricalEvidenceIntegrityError("unsupported v2 chunk format")
            try:
                if int(size) > HistoricalPriceRepository._v2_chunk_max_bytes:
                    raise ValueError("v2 chunk exceeds size limit")
                decompressor = zlib.decompressobj()
                encoded = decompressor.decompress(
                    bytes(compressed), HistoricalPriceRepository._v2_chunk_max_bytes + 1
                )
                if (
                    decompressor.unconsumed_tail
                    or not decompressor.eof
                    or len(encoded) > HistoricalPriceRepository._v2_chunk_max_bytes
                ):
                    raise ValueError("v2 chunk exceeds size limit")
                chunk = json.loads(encoded)
            except (TypeError, ValueError, zlib.error, json.JSONDecodeError) as exc:
                raise HistoricalEvidenceIntegrityError("invalid v2 chunk") from exc
            if (
                len(encoded) != int(size)
                or sha256(encoded).hexdigest() != str(digest)
                or chunk.get("kind") != str(kind)
                or chunk.get("year") != int(year)
                or not isinstance(chunk.get("items"), list)
            ):
                raise HistoricalEvidenceIntegrityError("v2 chunk integrity mismatch")
            (rows if str(kind) == "rows" else actions).extend(chunk["items"])
        if len(rows) != int(revision[3]) or len(actions) != int(revision[4]):
            raise HistoricalEvidenceIntegrityError("v2 evidence count mismatch")
        canonical = {**metadata, "rows": rows, "actions": actions}
        rendered = canonical_json(canonical)
        if canonical_json_digest(rendered) != data_revision:
            raise HistoricalEvidenceIntegrityError("v2 revision digest mismatch")
        return HistoricalPriceRepository._stored_from_canonical(
            data_revision, canonical, rendered, str(revision[2])
        )

    @staticmethod
    def _stored_from_canonical(
        data_revision: str,
        canonical: Mapping[str, object],
        rendered: str,
        response_metadata_digest: str,
    ) -> StoredHistoricalEvidence:
        try:
            request = canonical["request"]
            rows = canonical["rows"]
            actions = canonical["actions"]
            if (
                not isinstance(request, Mapping)
                or not isinstance(rows, list)
                or not isinstance(actions, list)
            ):
                raise TypeError
            return StoredHistoricalEvidence(
                data_revision=data_revision,
                security_id=str(canonical["security_id"]),
                provider=str(canonical["provider"]),
                provider_version=str(canonical["provider_version"]),
                request_contract_version=str(canonical["request_contract_version"]),
                requested_symbol=str(canonical["requested_symbol"]),
                observed_symbol=str(canonical["observed_symbol"]),
                alias_revision=None
                if canonical.get("alias_revision") is None
                else str(canonical["alias_revision"]),
                currency=str(canonical["currency"]),
                quote_unit=str(canonical["quote_unit"]),
                quote_unit_scale=str(canonical["quote_unit_scale"]),
                exchange_timezone=str(canonical["exchange_timezone"]),
                start=str(request["start"]),
                end=str(request["end"]),
                request_contract=dict(request),
                response_metadata_digest=response_metadata_digest,
                canonical_manifest_json=rendered,
                rows=tuple(rows),
                actions=tuple(actions),
            )
        except (KeyError, TypeError) as exc:
            raise HistoricalEvidenceIntegrityError("invalid v2 metadata") from exc

    @staticmethod
    def _load_on_connection(
        conn: sqlite3.Connection, data_revision: str
    ) -> StoredHistoricalEvidence:
        revision = conn.execute(
            """SELECT security_id, provider, provider_version,
                      request_contract_version, requested_symbol, observed_symbol,
                      alias_revision, currency, quote_unit, quote_unit_scale,
                      exchange_timezone, start_date, end_date, request_contract_json,
                      response_metadata_digest, canonical_manifest_json,
                      observation_count, action_count
               FROM historical_price_revisions WHERE data_revision=?""",
            (data_revision,),
        ).fetchone()
        if revision is None:
            raise EvidenceMissingError("historical evidence is missing")
        observation_rows = conn.execute(
            """SELECT session_date, open_hex, high_hex, low_hex, close_hex,
                      adj_close_hex, volume_hex, dividends_hex, stock_splits_hex
               FROM historical_price_observations WHERE data_revision=?
               ORDER BY session_date""",
            (data_revision,),
        ).fetchall()
        action_rows = conn.execute(
            """SELECT session_date, action_type, value_hex
               FROM historical_corporate_actions WHERE data_revision=?
               ORDER BY session_date,
                        CASE action_type WHEN 'dividend' THEN 0 ELSE 1 END""",
            (data_revision,),
        ).fetchall()
        if len(observation_rows) != int(revision[16]) or len(action_rows) != int(
            revision[17]
        ):
            raise HistoricalEvidenceIntegrityError("historical evidence count mismatch")
        rows = tuple(
            {
                "session": str(row[0]),
                "open": str(row[1]),
                "high": str(row[2]),
                "low": str(row[3]),
                "close": str(row[4]),
                "adj_close": None if row[5] is None else str(row[5]),
                "volume": str(row[6]),
                "dividends": str(row[7]),
                "stock_splits": str(row[8]),
            }
            for row in observation_rows
        )
        actions = tuple(
            {
                "session": str(row[0]),
                "action_type": str(row[1]),
                "value": str(row[2]),
            }
            for row in action_rows
        )
        return StoredHistoricalEvidence(
            data_revision=data_revision,
            security_id=str(revision[0]),
            provider=str(revision[1]),
            provider_version=str(revision[2]),
            request_contract_version=str(revision[3]),
            requested_symbol=str(revision[4]),
            observed_symbol=str(revision[5]),
            alias_revision=None if revision[6] is None else str(revision[6]),
            currency=str(revision[7]),
            quote_unit=str(revision[8]),
            quote_unit_scale=str(revision[9]),
            exchange_timezone=str(revision[10]),
            start=str(revision[11]),
            end=str(revision[12]),
            request_contract=json.loads(str(revision[13])),
            response_metadata_digest=str(revision[14]),
            canonical_manifest_json=str(revision[15]),
            rows=rows,
            actions=actions,
        )
