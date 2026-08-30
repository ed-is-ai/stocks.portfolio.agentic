"""Repository for the ``portfolio_recommendation_dispatches`` table (#442).

One row per (portfolio, analysis run) recommendation-email dispatch. The
composite primary key is the send-authority: ``claim`` is an atomic
``INSERT OR IGNORE``, so retrying or restarting the same published run can
never resend, while a later ``analysis_run_id`` claims fresh. Follows the
raw-sqlite house pattern used across ``app/repositories``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.repositories.db import Connect, session


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class PortfolioDispatchRepository:
    """Typed claim → sent/failed lifecycle for recommendation emails (#442)."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        """Create the receipt table if absent (idempotent, additive)."""
        with session(self._connect) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS portfolio_recommendation_dispatches ("
                "portfolio_id INTEGER NOT NULL, "
                "analysis_run_id TEXT NOT NULL, "
                "strategy_id TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'claimed', "
                "claimed_at TEXT NOT NULL, "
                "completed_at TEXT, "
                "PRIMARY KEY (portfolio_id, analysis_run_id))"
            )

    def claim(self, portfolio_id: int, analysis_run_id: str, strategy_id: str) -> bool:
        """Atomically claim one (portfolio, run) dispatch slot.

        Returns True when this call created the claim (caller should send);
        False when a row already exists for the pair — the run was already
        dispatched (or attempted) and must not resend.
        """
        with session(self._connect) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO portfolio_recommendation_dispatches "
                "(portfolio_id, analysis_run_id, strategy_id, status, claimed_at) "
                "VALUES (?, ?, ?, 'claimed', ?)",
                (portfolio_id, analysis_run_id, strategy_id, _utc_now()),
            )
            return cursor.rowcount > 0

    def mark_sent(self, portfolio_id: int, analysis_run_id: str) -> None:
        """Record a successful send for the claimed (portfolio, run) pair."""
        with session(self._connect) as conn:
            conn.execute(
                "UPDATE portfolio_recommendation_dispatches "
                "SET status = 'sent', completed_at = ? "
                "WHERE portfolio_id = ? AND analysis_run_id = ?",
                (_utc_now(), portfolio_id, analysis_run_id),
            )

    def mark_failed(self, portfolio_id: int, analysis_run_id: str) -> None:
        """Record a failed dispatch for the claimed (portfolio, run) pair."""
        with session(self._connect) as conn:
            conn.execute(
                "UPDATE portfolio_recommendation_dispatches "
                "SET status = 'failed', completed_at = ? "
                "WHERE portfolio_id = ? AND analysis_run_id = ?",
                (_utc_now(), portfolio_id, analysis_run_id),
            )

    def mark_skipped(self, portfolio_id: int, analysis_run_id: str) -> None:
        """Record a dispatch that legitimately sent nothing (no assignment)."""
        with session(self._connect) as conn:
            conn.execute(
                "UPDATE portfolio_recommendation_dispatches "
                "SET status = 'skipped', completed_at = ? "
                "WHERE portfolio_id = ? AND analysis_run_id = ?",
                (_utc_now(), portfolio_id, analysis_run_id),
            )

    def status_of(self, portfolio_id: int, analysis_run_id: str) -> str | None:
        """Return the receipt's status, or None when no row exists."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT status FROM portfolio_recommendation_dispatches "
                "WHERE portfolio_id = ? AND analysis_run_id = ?",
                (portfolio_id, analysis_run_id),
            ).fetchone()
        return str(row[0]) if row else None

    def was_sent(self, portfolio_id: int, analysis_run_id: str) -> bool:
        """Return True if the (portfolio, run) pair was already sent."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT 1 FROM portfolio_recommendation_dispatches "
                "WHERE portfolio_id = ? AND analysis_run_id = ? AND status = 'sent'",
                (portfolio_id, analysis_run_id),
            ).fetchone()
        return row is not None
