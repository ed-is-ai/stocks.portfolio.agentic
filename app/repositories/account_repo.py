"""Repository for the ``account_state`` key/value table in ``trades.db``."""

from datetime import datetime, timezone

from app.repositories.db import Connect, session


class AccountStateRepository:
    """Typed access to the ``account_state`` key/value store."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get(self, key: str) -> str | None:
        """Return the stored value for ``key``, or None if absent."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT value FROM account_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def exists(self, key: str) -> bool:
        """Return True if ``key`` has a stored value."""
        with session(self._connect) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM account_state WHERE key = ?", (key,)
                ).fetchone()
                is not None
            )

    def set(self, key: str, value: str) -> None:
        """Upsert ``key`` with ``value`` and a fresh UTC timestamp."""
        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with session(self._connect) as conn:
            conn.execute(
                "INSERT INTO account_state (key, value, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                "  value=excluded.value, updated_at=excluded.updated_at",
                (key, value, updated_at),
            )
            conn.commit()
