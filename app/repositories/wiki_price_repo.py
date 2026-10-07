"""Repository for the Quandl WIKI price archive in ``wiki_prices.db`` (#70).

WIKI is a separate provider from the yfinance cache: as-traded daily
``open, high, low, close, volume, ex_dividend, split_ratio`` per ticker up to
2018-03 (its ``adj_*`` columns are dropped). Tickers keep WIKI's spelling
(class shares use ``_``, e.g. ``BRK_B``). Dates are ISO strings.

An import replaces every price in one transaction and appends a
``wiki_imports`` record; re-importing a file with the same SHA-256 digest
writes nothing.
"""

import csv
import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.repositories.db import Connect, session

#: CSV header names of the stored columns, in ``wiki_prices`` column order.
CSV_COLUMNS = (
    "ticker",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ex-dividend",
    "split_ratio",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_imports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file   TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    ticker_count  INTEGER NOT NULL,
    first_date    TEXT,
    last_date     TEXT
);
CREATE TABLE IF NOT EXISTS wiki_prices (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    ex_dividend REAL,
    split_ratio REAL,
    PRIMARY KEY (ticker, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS wiki_tickers (
    ticker     TEXT PRIMARY KEY,
    first_date TEXT,
    last_date  TEXT,
    rows       INTEGER NOT NULL
);
"""

#: Per-ticker first/last date with a close (NULL when it never has one).
_TICKERS = """
INSERT INTO wiki_tickers
SELECT ticker, MIN(CASE WHEN close IS NOT NULL THEN date END),
       MAX(CASE WHEN close IS NOT NULL THEN date END), COUNT(*)
FROM wiki_prices GROUP BY ticker
"""


class WikiImport(BaseModel):
    """Provenance of one recorded WIKI import."""

    model_config = ConfigDict(frozen=True)

    id: int
    source_file: str
    source_digest: str
    imported_at: str
    row_count: int
    ticker_count: int
    first_date: str | None
    last_date: str | None


def file_digest(path: Path) -> str:
    """Return the SHA-256 hex digest of ``path``, read in a streaming pass."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class WikiPriceRepository:
    """Store of the WIKI price archive with its import records."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        """Create the tables if missing (idempotent)."""
        with session(self._connect) as conn:
            conn.executescript(_SCHEMA)

    def latest_import(self) -> WikiImport | None:
        """Return the newest import record, if any."""
        with session(self._connect) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM wiki_imports ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else WikiImport(**dict(row))

    def import_csv(self, path: Path) -> tuple[WikiImport, bool]:
        """Import a WIKI CSV; return ``(import, created)``.

        Rows are streamed into one ``executemany`` inside one transaction. When
        the newest import has the file's digest nothing is written and that
        import is returned with ``False``. Raises ``ValueError`` (nothing
        written) naming any stored column missing from the header, a short
        row's line, a duplicate ticker/date, or a file without price rows.
        """
        digest = file_digest(path)
        latest = self.latest_import()
        if latest is not None and latest.source_digest == digest:
            return latest, False
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            indexes = _column_indexes(next(reader, []))
            rows = _stored_fields(reader, indexes)
            with session(self._connect) as conn:
                # Bulk-load tuning for this connection only. The rollback
                # journal stays on, so a failed or killed import rolls back;
                # an OS crash or power loss mid-import can corrupt the file
                # (re-import from the CSV).
                conn.execute("PRAGMA synchronous = OFF")
                conn.execute("PRAGMA cache_size = -262144")
                try:
                    record = _replace_prices(conn, rows, path.name, digest)
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"duplicate ticker/date in {path}") from exc
        return record, True

    def ticker_spans(self) -> dict[str, tuple[str, str]]:
        """Return each ticker's first and last date with a close."""
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT ticker, first_date, last_date FROM wiki_tickers"
                " WHERE first_date IS NOT NULL"
            ).fetchall()
        return {t: (first, last) for t, first, last in rows}

    def closes(self, ticker: str, year: int) -> dict[str, tuple[float, float]]:
        """Return ``ticker``'s non-null ``(close, split_ratio)`` in ``year`` by
        date (a missing split ratio reads as 1)."""
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT date, close, COALESCE(split_ratio, 1.0) FROM wiki_prices"
                " WHERE ticker = ? AND date BETWEEN ? AND ? AND close IS NOT NULL",
                (ticker, f"{year}-01-01", f"{year}-12-31"),
            ).fetchall()
        return {day: (close, ratio) for day, close, ratio in rows}


def _stored_fields(
    reader: Iterator[list[str]], indexes: list[int]
) -> Iterator[list[str | None]]:
    """Yield the stored fields of each row (blank -> ``None``); raise
    ``ValueError`` naming the line of a row shorter than the header."""
    width = max(indexes) + 1
    for line, row in enumerate(reader, 2):
        if not row:
            continue
        if len(row) < width:
            raise ValueError(f"line {line}: expected {width}+ fields")
        yield [row[i] or None for i in indexes]


def _column_indexes(header: list[str]) -> list[int]:
    missing = [c for c in CSV_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")
    return [header.index(c) for c in CSV_COLUMNS]


def _replace_prices(
    conn: sqlite3.Connection,
    rows: Iterable[list[str | None]],
    source_file: str,
    digest: str,
) -> WikiImport:
    """Replace every price with ``rows`` (streamed) and record the import."""
    conn.execute("DELETE FROM wiki_prices")
    conn.execute("DELETE FROM wiki_tickers")
    count = conn.executemany(
        f"INSERT INTO wiki_prices VALUES ({', '.join('?' * len(CSV_COLUMNS))})",
        rows,
    ).rowcount
    if count < 1:
        raise ValueError(f"{source_file} has no price rows")
    conn.execute(_TICKERS)
    tickers, first, last = conn.execute(
        "SELECT COUNT(*), MIN(first_date), MAX(last_date) FROM wiki_tickers"
    ).fetchone()
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = (source_file, digest, imported_at, count, tickers, first, last)
    cursor = conn.execute(
        "INSERT INTO wiki_imports (source_file, source_digest, imported_at,"
        " row_count, ticker_count, first_date, last_date)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        values,
    )
    assert cursor.lastrowid is not None
    return WikiImport(
        id=cursor.lastrowid,
        source_file=source_file,
        source_digest=digest,
        imported_at=imported_at,
        row_count=count,
        ticker_count=tickers,
        first_date=first,
        last_date=last,
    )
