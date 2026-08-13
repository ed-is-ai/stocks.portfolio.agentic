"""Repository for ``notifications.db`` — the notification centre store (#80).

Single owner of the ``notifications`` table schema. Follows the raw-sqlite
pattern used across ``app/repositories``: a zero-argument ``Connect`` factory,
the ``session`` context manager, and additive migrations. Presentation logic
lives on the ``Notification`` schema; retention policy (prune older than N
days) lives here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.core import config
from app.repositories import db
from app.repositories.db import Connect, session
from app.schemas.notification import (
    Notification,
    NotificationCategory,
    NotificationSeverity,
)

#: Notifications older than this are pruned on write (#81 decision).
DEFAULT_RETENTION_DAYS = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'info',
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    ticker       TEXT,
    run_id       TEXT,
    created_at   TEXT NOT NULL,
    read_at      TEXT,
    dismissed_at TEXT
)
"""

_COLUMNS = (
    "id",
    "category",
    "event_type",
    "severity",
    "title",
    "body",
    "ticker",
    "run_id",
    "created_at",
    "read_at",
    "dismissed_at",
    "portfolio_id",
    "job_id",
    "job_status_version",
    "target_url",
    "actions_json",
)


def _row_to_notification(row: tuple) -> Notification:
    """Map a raw ``notifications`` row (in ``_COLUMNS`` order) to the schema."""
    import json

    data = dict(zip(_COLUMNS, row))
    raw_actions = data.pop("actions_json", "[]")
    try:
        data["actions"] = tuple(json.loads(raw_actions or "[]"))
    except (TypeError, json.JSONDecodeError):
        data["actions"] = ()
    return Notification.model_validate(data)


class NotificationsRepository:
    """Typed access to the ``notifications`` table in ``notifications.db``."""

    def __init__(
        self, connect: Connect, retention_days: int = DEFAULT_RETENTION_DAYS
    ) -> None:
        self._connect = connect
        self._retention_days = retention_days

    def ensure_schema(self) -> None:
        """Create the notifications table if it does not yet exist."""
        with session(self._connect) as conn:
            conn.execute(_SCHEMA)
            try:
                conn.execute(
                    "ALTER TABLE notifications ADD COLUMN portfolio_id INTEGER"
                )
            except sqlite3.OperationalError:
                pass  # column already exists (idempotent, matches trades.db)
            for name, definition in (
                ("job_id", "TEXT"),
                ("job_status_version", "INTEGER"),
                ("target_url", "TEXT"),
                ("actions_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                try:
                    conn.execute(
                        f"ALTER TABLE notifications ADD COLUMN {name} {definition}"
                    )
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS notifications_job_id_unique "
                "ON notifications(job_id) WHERE job_id IS NOT NULL"
            )
            conn.commit()

    def record(
        self,
        category: NotificationCategory,
        event_type: str,
        title: str,
        *,
        severity: NotificationSeverity = NotificationSeverity.INFO,
        body: str = "",
        ticker: str | None = None,
        run_id: str | None = None,
        portfolio_id: int | None = None,
    ) -> int:
        """Insert a notification and prune expired rows; return the new id.

        ``portfolio_id`` ties an event to the account it's about (e.g. a
        SIPP import or the account's own deletion, #186) for reference —
        events are kept as history even after that account is gone, so
        leave unset for events that aren't account-scoped (alerts,
        pipeline runs).
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with session(self._connect) as conn:
            cursor = conn.execute(
                "INSERT INTO notifications"
                " (category, event_type, severity, title, body, ticker,"
                "  run_id, created_at, portfolio_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(category),
                    event_type,
                    str(severity),
                    title,
                    body,
                    ticker,
                    run_id,
                    created_at,
                    portfolio_id,
                ),
            )
            new_id = int(cursor.lastrowid or 0)
            self._prune(conn, self._retention_days)
            conn.commit()
        return new_id

    def upsert_job_notification(
        self,
        *,
        job_id: str,
        job_status_version: int,
        category: NotificationCategory,
        event_type: str,
        severity: NotificationSeverity,
        title: str,
        body: str,
        created_at: datetime,
        target_url: str | None,
        actions: tuple[str, ...],
    ) -> int:
        """Compare-and-upsert one mutable lifecycle notification by job."""
        import json

        with session(self._connect) as conn:
            existing = conn.execute(
                "SELECT id, job_status_version FROM notifications WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if existing is not None and int(existing[1] or 0) > job_status_version:
                raise ValueError("notification projection version is newer")
            if existing is None:
                cursor = conn.execute(
                    """INSERT INTO notifications (
                         category,event_type,severity,title,body,run_id,created_at,
                         job_id,job_status_version,target_url,actions_json
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(category),
                        event_type,
                        str(severity),
                        title,
                        body,
                        None,
                        created_at.isoformat(),
                        job_id,
                        job_status_version,
                        target_url,
                        json.dumps(actions, separators=(",", ":")),
                    ),
                )
                result = int(cursor.lastrowid or 0)
            else:
                conn.execute(
                    """UPDATE notifications SET category=?, event_type=?, severity=?,
                         title=?, body=?, created_at=?, job_status_version=?,
                         target_url=?, actions_json=? WHERE id=?""",
                    (
                        str(category),
                        event_type,
                        str(severity),
                        title,
                        body,
                        created_at.isoformat(),
                        job_status_version,
                        target_url,
                        json.dumps(actions, separators=(",", ":")),
                        int(existing[0]),
                    ),
                )
                result = int(existing[0])
            self._prune(conn, self._retention_days)
            conn.commit()
            return result

    def recent(
        self, limit: int = 50, include_dismissed: bool = False
    ) -> list[Notification]:
        """Return the most recent notifications, newest first."""
        clause = "" if include_dismissed else " WHERE dismissed_at IS NULL"
        with session(self._connect) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM notifications{clause}"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_notification(row) for row in rows]

    def unread_count(self) -> int:
        """Return the number of unread, non-dismissed notifications."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM notifications"
                " WHERE read_at IS NULL AND dismissed_at IS NULL"
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_read(self, notification_id: int) -> None:
        """Mark a single notification read (idempotent)."""
        now = datetime.now(timezone.utc).isoformat()
        with session(self._connect) as conn:
            conn.execute(
                "UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL",
                (now, notification_id),
            )
            conn.commit()

    def mark_all_read(self) -> None:
        """Mark every unread notification read."""
        now = datetime.now(timezone.utc).isoformat()
        with session(self._connect) as conn:
            conn.execute(
                "UPDATE notifications SET read_at=? WHERE read_at IS NULL",
                (now,),
            )
            conn.commit()

    def dismiss(self, notification_id: int) -> None:
        """Dismiss a single notification so it drops out of the feed."""
        now = datetime.now(timezone.utc).isoformat()
        with session(self._connect) as conn:
            conn.execute(
                "UPDATE notifications SET dismissed_at=?"
                " WHERE id=? AND dismissed_at IS NULL",
                (now, notification_id),
            )
            conn.commit()

    def prune_older_than(self, days: int) -> int:
        """Delete notifications older than ``days``; return rows removed."""
        with session(self._connect) as conn:
            removed = self._prune(conn, days)
            conn.commit()
        return removed

    @staticmethod
    def _prune(conn, days: int) -> int:
        """Delete rows created before the retention cutoff on ``conn``."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = conn.execute(
            "DELETE FROM notifications WHERE created_at < ?", (cutoff,)
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def build_notifications_repository() -> NotificationsRepository:
    """Return a schema-ready repository against the configured store.

    Used by non-web writers (the orchestrator and ``AlertAgent``) that do not
    go through FastAPI dependency injection. Reads the path lazily so tests
    that repoint ``NOTIFICATIONS_DB`` are honoured.
    """
    connect = db.make_connect(lambda: str(config.NOTIFICATIONS_DB))
    repo = NotificationsRepository(connect)
    repo.ensure_schema()
    return repo
