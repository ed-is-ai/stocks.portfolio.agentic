"""Database connection factory and schema for ``trades.db``.

This module is the single owner of the ``trades.db`` schema (tables and
migrations). Repositories receive a zero-argument ``Connect`` callable so they
always open a connection against the current path, even if a caller reassigns
its database path after construction (as the trader tests do).
"""

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

#: A zero-argument factory returning a fresh SQLite connection.
Connect = Callable[[], sqlite3.Connection]


@contextmanager
def session(connect: "Connect") -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, and always close it.

    Unlike ``with sqlite3.connect(...)`` (which commits but leaves the
    connection open), this releases the file handle — important on Windows
    where an open handle keeps the database file locked.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK(action IN ('BUY', 'SELL')),
    shares      REAL NOT NULL CHECK(shares > 0),
    price       REAL NOT NULL CHECK(price > 0),
    date        TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    stop_loss   REAL,
    entry_price REAL,
    reference   TEXT
);
CREATE TABLE IF NOT EXISTS cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    flow_type   TEXT NOT NULL CHECK(flow_type IN ('CONTRIBUTION', 'DIVIDEND', 'INTEREST', 'TAX_RELIEF', 'TRANSFER', 'WITHDRAWAL', 'OTHER')),
    ticker      TEXT,
    amount      REAL NOT NULL CHECK(amount > 0),
    description TEXT,
    reference   TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS price_cache (
    ticker          TEXT PRIMARY KEY,
    price           REAL NOT NULL,
    fetched_at      TEXT NOT NULL,
    currency        TEXT DEFAULT 'GBP',
    original_price  REAL
);
CREATE TABLE IF NOT EXISTS account_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection to ``db_path``."""
    return sqlite3.connect(db_path)


def make_connect(db_path_getter: Callable[[], str | Path]) -> Connect:
    """Return a ``Connect`` factory that reads the path lazily on each call."""
    return lambda: connect(db_path_getter())


def init_trades_db(conn: sqlite3.Connection) -> None:
    """Create the ``trades.db`` schema and apply additive migrations."""
    conn.executescript(_SCHEMA)
    for col_def in ("stop_loss REAL", "entry_price REAL", "reference TEXT"):
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col_def}")
        except sqlite3.OperationalError as exc:
            logger.debug("schema migration step skipped: %s", exc)
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_reference "
            "ON trades(reference) WHERE reference IS NOT NULL"
        )
    except sqlite3.OperationalError as exc:
        logger.debug("index migration step skipped: %s", exc)
    for col_def in ("currency TEXT DEFAULT 'GBP'", "original_price REAL"):
        try:
            conn.execute(f"ALTER TABLE price_cache ADD COLUMN {col_def}")
        except sqlite3.OperationalError as exc:
            logger.debug("schema migration step skipped: %s", exc)
    conn.commit()
