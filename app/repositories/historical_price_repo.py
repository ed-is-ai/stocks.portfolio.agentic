"""Immutable persistence for provider-native historical market evidence."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Mapping

from app.repositories.db import Connect, session
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.historical_price_evidence import HistoricalEvidencePayload

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
"""


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


class HistoricalPriceRepository:
    """Own the append-only historical price database and exact-reference reads."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        with session(self._connect) as conn:
            conn.executescript(_SCHEMA)

    def commit(self, payload: HistoricalEvidencePayload) -> str:
        try:
            return self._commit(payload)
        except HistoricalEvidenceIntegrityError:
            raise
        except sqlite3.IntegrityError as exc:
            raise HistoricalEvidenceIntegrityError(
                "historical evidence transaction failed"
            ) from exc

    def _commit(self, payload: HistoricalEvidencePayload) -> str:
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

        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
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
        return payload.data_revision

    def get(self, data_revision: str) -> StoredHistoricalEvidence:
        with session(self._connect) as conn:
            return self._load_on_connection(conn, data_revision)

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
    ) -> StoredHistoricalEvidence | None:
        """Return the earliest immutable revision for one exact request identity."""
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT data_revision FROM historical_price_revisions
                   WHERE security_id=? AND requested_symbol=?
                     AND alias_revision IS ? AND start_date=? AND end_date=?
                     AND request_contract_version=?
                   ORDER BY first_acquired_at, data_revision LIMIT 1""",
                (
                    security_id,
                    requested_symbol,
                    alias_revision,
                    start,
                    end,
                    request_contract_version,
                ),
            ).fetchone()
            if row is None:
                return None
            return self._verify_on_connection(conn, str(row[0]))

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
        if manifest_digest(canonical) != data_revision:
            raise HistoricalEvidenceIntegrityError("stored revision digest mismatch")
        if canonical.get("rows") != list(evidence.rows):
            raise HistoricalEvidenceIntegrityError("stored observation mismatch")
        if canonical.get("actions") != list(evidence.actions):
            raise HistoricalEvidenceIntegrityError("stored action mismatch")
        return evidence

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
