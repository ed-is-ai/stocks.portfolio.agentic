"""Repository for point-in-time index membership in ``index_membership.db`` (#68).

Conventions:

* An interval is ``[start_date, end_date)``: a ticker is a member on
  ``start_date`` and is no longer a member on ``end_date``. ``end_date`` is
  ``None`` while it is still a member at the source's last date.
* Dates are ISO ``YYYY-MM-DD`` strings, so string comparison is date order.
* Imports are append-only. Reads always use the newest import of an index;
  re-importing an unchanged source (same digest) records nothing new.
* Confidence belongs to a date, not an interval: a roster dated before the
  import's ``low_confidence_before`` is ``low``.
"""

import sqlite3
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.repositories.db import Connect, session

Confidence = Literal["low", "normal"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS membership_imports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    index_id       TEXT NOT NULL,
    source         TEXT NOT NULL,
    source_ref     TEXT NOT NULL,
    source_digest  TEXT NOT NULL,
    imported_at    TEXT NOT NULL,
    first_date     TEXT NOT NULL,
    last_date      TEXT NOT NULL,
    snapshot_count INTEGER NOT NULL,
    interval_count INTEGER NOT NULL,
    low_confidence_before TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS membership_intervals (
    import_id    INTEGER NOT NULL REFERENCES membership_imports(id),
    index_id     TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT,
    security_key TEXT NOT NULL,
    PRIMARY KEY (import_id, security_key)
);
CREATE INDEX IF NOT EXISTS idx_membership_intervals_ticker
    ON membership_intervals (import_id, ticker);
"""

_INTERVAL_COLUMNS = "ticker, start_date, end_date, security_key"


class MembershipInterval(BaseModel):
    """One contiguous membership spell of one ticker (``[start, end)``)."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    start_date: str
    end_date: str | None
    security_key: str


class MembershipImport(BaseModel):
    """Provenance of one recorded import of an index's membership."""

    model_config = ConfigDict(frozen=True)

    id: int
    index_id: str
    source: str
    source_ref: str
    source_digest: str
    imported_at: str
    first_date: str
    last_date: str
    snapshot_count: int
    interval_count: int
    low_confidence_before: str


class Roster(BaseModel):
    """An index's members on one date, with the import it came from."""

    model_config = ConfigDict(frozen=True)

    as_of: str
    source: MembershipImport
    confidence: Confidence
    #: ``as_of`` is after the source's last date, so members may be out of date.
    stale: bool
    members: list[MembershipInterval]


class IndexMembershipRepository:
    """Append-only store of index membership intervals with import records."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        """Create the tables if missing (idempotent)."""
        with session(self._connect) as conn:
            conn.executescript(_SCHEMA)

    def record_import(
        self,
        *,
        index_id: str,
        source: str,
        source_ref: str,
        source_digest: str,
        first_date: str,
        last_date: str,
        snapshot_count: int,
        low_confidence_before: str,
        intervals: list[MembershipInterval],
    ) -> tuple[int, bool]:
        """Store an import and its intervals; return ``(import_id, created)``.

        When the newest import of ``index_id`` already has ``source_digest``
        nothing is written and that import's id is returned with ``False``.
        """
        latest = self.latest_import(index_id)
        if latest is not None and latest.source_digest == source_digest:
            return latest.id, False
        imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with session(self._connect) as conn:
            cursor = conn.execute(
                "INSERT INTO membership_imports (index_id, source, source_ref,"
                " source_digest, imported_at, first_date, last_date,"
                " snapshot_count, interval_count, low_confidence_before)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    index_id,
                    source,
                    source_ref,
                    source_digest,
                    imported_at,
                    first_date,
                    last_date,
                    snapshot_count,
                    len(intervals),
                    low_confidence_before,
                ),
            )
            import_id = cursor.lastrowid
            assert import_id is not None
            conn.executemany(
                f"INSERT INTO membership_intervals (import_id, index_id,"
                f" {_INTERVAL_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        import_id,
                        index_id,
                        i.ticker,
                        i.start_date,
                        i.end_date,
                        i.security_key,
                    )
                    for i in intervals
                ],
            )
        return import_id, True

    def latest_import(self, index_id: str) -> MembershipImport | None:
        """Return the newest import record of ``index_id``, if any."""
        with session(self._connect) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM membership_imports WHERE index_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (index_id,),
            ).fetchone()
        return None if row is None else MembershipImport(**dict(row))

    def roster_on(self, index_id: str, as_of: str) -> Roster | None:
        """Return the members on ``as_of`` (start inclusive, end exclusive) in
        the newest import, ordered by ticker; ``None`` if never imported.

        Raises ``ValueError`` if ``as_of`` is not an ISO date.
        """
        as_of = date.fromisoformat(as_of).isoformat()
        latest = self.latest_import(index_id)
        if latest is None:
            return None
        members = self._intervals(
            latest.id,
            "start_date <= ? AND (end_date IS NULL OR end_date > ?)",
            (as_of, as_of),
        )
        low = as_of < latest.low_confidence_before
        return Roster(
            as_of=as_of,
            source=latest,
            confidence="low" if low else "normal",
            stale=as_of > latest.last_date,
            members=members,
        )

    def intervals_for(self, index_id: str, ticker: str) -> list[MembershipInterval]:
        """Return every interval of ``ticker`` in the newest import."""
        latest = self.latest_import(index_id)
        if latest is None:
            return []
        return self._intervals(latest.id, "ticker = ?", (ticker,))

    def _intervals(
        self, import_id: int, where: str, params: tuple[str, ...]
    ) -> list[MembershipInterval]:
        with session(self._connect) as conn:
            rows = conn.execute(
                f"SELECT {_INTERVAL_COLUMNS} FROM membership_intervals"
                f" WHERE import_id = ? AND {where} ORDER BY ticker, start_date",
                (import_id, *params),
            ).fetchall()
        return [
            MembershipInterval(
                ticker=r[0], start_date=r[1], end_date=r[2], security_key=r[3]
            )
            for r in rows
        ]
