"""Repository for ``alerts.db`` — the AlertAgent's watch/cooldown state.

Single owner of the ``alerts`` table schema. The cooldown *policy* stays in the
agent; this repository only exposes the queries it needs.
"""

from datetime import datetime, timezone
from typing import Any

from app.repositories.db import Connect, session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    ticker TEXT NOT NULL,
    alerted_at TEXT NOT NULL,
    score INTEGER,
    stage TEXT,
    summary TEXT
)
"""
_MIGRATIONS = (
    "ALTER TABLE alerts ADD COLUMN entry_price REAL",
    "ALTER TABLE alerts ADD COLUMN stop_loss REAL",
    "ALTER TABLE alerts ADD COLUMN status TEXT DEFAULT 'watching'",
)


class AlertsRepository:
    """Typed access to the ``alerts`` table in ``alerts.db``."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        """Create the alerts table and apply additive migrations."""
        with session(self._connect) as conn:
            conn.execute(_SCHEMA)
            for sql in _MIGRATIONS:
                try:
                    conn.execute(sql)
                except Exception:
                    pass
            conn.commit()

    def clear(self) -> None:
        """Delete every alert row (run-start reset)."""
        with session(self._connect) as conn:
            conn.execute("DELETE FROM alerts")
            conn.commit()

    def watching(self) -> list[tuple[Any, ...]]:
        """Return (rowid, ticker, entry_price, stop_loss) for watching alerts."""
        with session(self._connect) as conn:
            return conn.execute(
                "SELECT rowid, ticker, entry_price, stop_loss"
                " FROM alerts WHERE status='watching'"
            ).fetchall()

    def set_status(self, rowid: int, status: str) -> None:
        """Update the status of a single alert row."""
        with session(self._connect) as conn:
            conn.execute("UPDATE alerts SET status=? WHERE rowid=?", (status, rowid))
            conn.commit()

    def has_watching(self, ticker: str) -> bool:
        """Return True if ``ticker`` has a watching alert."""
        with session(self._connect) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM alerts WHERE ticker=? AND status='watching' LIMIT 1",
                    (ticker,),
                ).fetchone()
                is not None
            )

    def last_alerted_at(self, ticker: str) -> str | None:
        """Return the most recent ``alerted_at`` for ``ticker``, or None."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT alerted_at FROM alerts WHERE ticker=?"
                " ORDER BY alerted_at DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        return row[0] if row else None

    def record(
        self,
        ticker: str,
        score: int | None,
        stage: str | None,
        summary: str | None,
        entry_price: float | None,
        stop_loss: float | None,
    ) -> None:
        """Insert a new 'watching' alert with the current UTC timestamp."""
        with session(self._connect) as conn:
            conn.execute(
                "INSERT INTO alerts"
                " (ticker, alerted_at, score, stage, summary, entry_price,"
                "  stop_loss, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'watching')",
                (
                    ticker,
                    datetime.now(timezone.utc).isoformat(),
                    score,
                    stage,
                    summary,
                    entry_price,
                    stop_loss,
                ),
            )
            conn.commit()
