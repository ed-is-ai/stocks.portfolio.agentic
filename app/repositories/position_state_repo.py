"""Repository for ``position_state.db`` — per-position run-to-run state (#82).

Single owner of the ``position_state`` table, which persists a high-water-mark
(the highest price observed since entry) per held position. ``Position`` is
rebuilt every pipeline run from trade replay and carries no run-to-run state,
so the trailing-stop peak must live in a dedicated store. Follows the raw-sqlite
idiom used across ``app/repositories``: a zero-argument ``Connect`` factory, the
``session`` context manager, additive migrations, and a ``build_*`` factory that
reads the configured path lazily so tests can repoint it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core import config
from app.repositories import db
from app.repositories.db import Connect, session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_state (
    ticker          TEXT PRIMARY KEY,
    high_water_mark REAL NOT NULL,
    updated_at      TEXT NOT NULL
)
"""


class PositionStateRepository:
    """Typed access to the ``position_state`` table in ``position_state.db``."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        """Create the position_state table if it does not yet exist."""
        with session(self._connect) as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def get_hwm(self, ticker: str) -> float | None:
        """Return the stored high-water-mark for ``ticker``, or None."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT high_water_mark FROM position_state WHERE ticker=?",
                (ticker,),
            ).fetchone()
        return float(row[0]) if row else None

    def record_high(self, ticker: str, price: float) -> float:
        """Raise the stored peak to ``max(existing, price)`` and return it.

        The high-water-mark only ever rises: a new low leaves the stored peak
        untouched. Returns the resulting high-water-mark.
        """
        now = datetime.now(timezone.utc).isoformat()
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT high_water_mark FROM position_state WHERE ticker=?",
                (ticker,),
            ).fetchone()
            new_hwm = max(float(row[0]), price) if row else price
            conn.execute(
                "INSERT INTO position_state (ticker, high_water_mark, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(ticker) DO UPDATE SET"
                " high_water_mark=excluded.high_water_mark,"
                " updated_at=excluded.updated_at",
                (ticker, new_hwm, now),
            )
            conn.commit()
        return new_hwm

    def prune(self, held_tickers: set[str]) -> int:
        """Delete peaks for tickers no longer held; return rows removed."""
        with session(self._connect) as conn:
            rows = conn.execute("SELECT ticker FROM position_state").fetchall()
            stale = [r[0] for r in rows if r[0] not in held_tickers]
            for ticker in stale:
                conn.execute("DELETE FROM position_state WHERE ticker=?", (ticker,))
            conn.commit()
        return len(stale)


def build_position_state_repository() -> PositionStateRepository:
    """Return a schema-ready repository against the configured store.

    Reads the path lazily so tests that repoint ``POSITION_STATE_DB`` are
    honoured (mirrors ``build_notifications_repository``).
    """
    connect = db.make_connect(lambda: str(config.POSITION_STATE_DB))
    repo = PositionStateRepository(connect)
    repo.ensure_schema()
    return repo
