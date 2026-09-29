"""Repository for ``position_theses`` and ``thesis_evaluations`` (GH-14).

Thesis versions are immutable rows: a save adds a version and only the
``active`` flag (plus ``confirmed_at`` on confirmation) ever changes. The
partial unique index ``idx_position_theses_one_active`` enforces at most one
active version per (portfolio, security). Evaluations are append-only,
keyed ``UNIQUE(thesis_id, analysis_run_id)`` and written ``INSERT OR IGNORE``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from app.repositories.db import Connect, session
from app.schemas.position_thesis import (
    RULE_ADAPTER,
    PositionThesisV1,
    ThesisContentV1,
    ThesisEvaluationV1,
    ThesisTextSource,
)

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 10

_COLUMNS = (
    "id, portfolio_id, security_id, version, rationale, expected_setup, "
    "rules_json, review_date, text_source, active, confirmed_at, created_at"
)
#: A pending draft: the holding's newest version, AI-drafted, never confirmed.
_PENDING = (
    "active = 0 AND text_source = 'ai_draft' AND confirmed_at IS NULL "
    "AND version = (SELECT MAX(version) FROM position_theses "
    "WHERE portfolio_id = t.portfolio_id AND security_id = t.security_id)"
)


class StaleDraftError(LookupError):
    """The version id is not that holding's pending draft."""


class VersionConflictError(RuntimeError):
    """The holding's newest version is not the one the caller expected."""


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_thesis(row: tuple[Any, ...]) -> PositionThesisV1:
    return PositionThesisV1(
        id=int(row[0]),
        portfolio_id=int(row[1]),
        security_id=str(row[2]),
        version=int(row[3]),
        rationale=str(row[4]),
        expected_setup=str(row[5]),
        rules=tuple(RULE_ADAPTER.validate_python(r) for r in json.loads(row[6])),
        review_date=row[7],
        text_source=row[8],
        active=bool(row[9]),
        confirmed_at=row[10],
        created_at=str(row[11]),
    )


def _by_security(rows: Iterable[tuple[Any, ...]]) -> dict[str, PositionThesisV1]:
    """Parse rows keyed by security, skipping (and logging) corrupt ones."""
    theses: dict[str, PositionThesisV1] = {}
    for row in rows:
        try:
            theses[str(row[2])] = _row_to_thesis(row)
        except Exception:
            logger.warning("Skipping unreadable thesis row %s", row[0], exc_info=True)
    return theses


def _evaluation(facts_json: str) -> ThesisEvaluationV1 | None:
    """Parse one evaluation's facts, or None (logged) when unreadable."""
    try:
        return ThesisEvaluationV1.model_validate_json(facts_json)
    except Exception:
        logger.warning("Skipping unreadable thesis evaluation", exc_info=True)
        return None


class PositionThesesRepository:
    """Typed access to thesis versions and their evaluation history."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def add_version(
        self,
        portfolio_id: int,
        security_id: str,
        content: ThesisContentV1,
        text_source: ThesisTextSource,
        *,
        active: bool,
        expected_latest_version: int | None = None,
    ) -> PositionThesisV1:
        """Insert the holding's next version; an active one supersedes others.

        Deactivation and insert share one ``BEGIN IMMEDIATE`` transaction, so
        the one-active index never sees two active rows. With
        ``expected_latest_version``, raises :class:`VersionConflictError`
        (nothing written) when the newest version differs, checked in that
        same transaction.
        """
        now = _utc_now()
        rules = json.dumps(
            [rule.model_dump(mode="json") for rule in content.rules],
            separators=(",", ":"),
        )
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_latest_version is not None:
                latest = self._latest(conn, portfolio_id, security_id)
                if latest != expected_latest_version:
                    raise VersionConflictError(
                        f"newest version is {latest}, "
                        f"expected {expected_latest_version}"
                    )
            if active:
                self._deactivate(conn, portfolio_id, security_id)
            cur = conn.execute(
                "INSERT INTO position_theses (portfolio_id, security_id, version, "
                "rationale, expected_setup, rules_json, review_date, text_source, "
                "active, confirmed_at, created_at) VALUES (?, ?, "
                "(SELECT COALESCE(MAX(version), 0) + 1 FROM position_theses "
                "WHERE portfolio_id = ? AND security_id = ?), "
                "?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    portfolio_id,
                    security_id,
                    portfolio_id,
                    security_id,
                    content.rationale,
                    content.expected_setup,
                    rules,
                    content.review_date.isoformat() if content.review_date else None,
                    text_source,
                    int(active),
                    now if active else None,
                    now,
                ),
            )
            row = self._get(conn, int(cur.lastrowid or 0))
        if row is None:  # pragma: no cover — the insert above just wrote it
            raise RuntimeError("thesis version disappeared after insert")
        return row

    def activate_pending_draft(
        self, portfolio_id: int, security_id: str, thesis_id: int
    ) -> PositionThesisV1:
        """Confirm the holding's pending draft as its only active version.

        The pending check and the activation share one ``BEGIN IMMEDIATE``
        transaction, so a save committed in between cannot be overridden.
        Raises :class:`StaleDraftError` unless ``thesis_id`` is still the
        holding's pending draft.
        """
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                f"SELECT 1 FROM position_theses AS t WHERE id = ? "
                f"AND portfolio_id = ? AND security_id = ? AND {_PENDING}",
                (thesis_id, portfolio_id, security_id),
            ).fetchone()
            if pending is None:
                raise StaleDraftError(f"thesis {thesis_id} is not a pending draft")
            self._deactivate(conn, portfolio_id, security_id)
            conn.execute(
                "UPDATE position_theses SET active = 1, confirmed_at = ? WHERE id = ?",
                (_utc_now(), thesis_id),
            )
            thesis = self._get(conn, thesis_id)
        if thesis is None:  # pragma: no cover — checked in this transaction
            raise StaleDraftError(f"thesis {thesis_id} disappeared")
        return thesis

    def latest_version(self, portfolio_id: int, security_id: str) -> int:
        """Return the holding's newest version number (0 when it has none)."""
        with session(self._connect) as conn:
            return self._latest(conn, portfolio_id, security_id)

    def active_for_portfolio(self, portfolio_id: int) -> dict[str, PositionThesisV1]:
        """Return each security's active version for the portfolio."""
        with session(self._connect) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM position_theses "
                "WHERE portfolio_id = ? AND active = 1",
                (portfolio_id,),
            ).fetchall()
        return _by_security(rows)

    def pending_drafts(self, portfolio_id: int) -> dict[str, PositionThesisV1]:
        """Return each security's pending AI draft.

        A draft is pending while it is the holding's newest version and has
        never been confirmed; any later save supersedes it.
        """
        with session(self._connect) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM position_theses AS t "
                f"WHERE portfolio_id = ? AND {_PENDING}",
                (portfolio_id,),
            ).fetchall()
        return _by_security(rows)

    def append_evaluation(self, evaluation: ThesisEvaluationV1) -> bool:
        """Append one evaluation; False when this run was already recorded."""
        with session(self._connect) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO thesis_evaluations (thesis_id, "
                "analysis_run_id, status, facts_json, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    evaluation.thesis_id,
                    evaluation.analysis_run_id,
                    evaluation.status,
                    evaluation.model_dump_json(),
                    _utc_now(),
                ),
            )
            return cur.rowcount > 0

    def latest_evaluations(
        self, thesis_ids: list[int]
    ) -> dict[int, ThesisEvaluationV1]:
        """Return the newest evaluation of each given thesis version."""
        if not thesis_ids:
            return {}
        marks = ", ".join("?" for _ in thesis_ids)
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT thesis_id, facts_json FROM thesis_evaluations "
                f"WHERE id IN (SELECT MAX(id) FROM thesis_evaluations "
                f"WHERE thesis_id IN ({marks}) GROUP BY thesis_id)",
                thesis_ids,
            ).fetchall()
        return {
            int(row[0]): evaluation
            for row in rows
            if (evaluation := _evaluation(row[1])) is not None
        }

    def history(
        self, portfolio_id: int, security_id: str, limit: int = HISTORY_LIMIT
    ) -> list[ThesisEvaluationV1]:
        """Return the holding's newest evaluations across all versions."""
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT e.facts_json FROM thesis_evaluations AS e "
                "JOIN position_theses AS t ON t.id = e.thesis_id "
                "WHERE t.portfolio_id = ? AND t.security_id = ? "
                "ORDER BY e.id DESC LIMIT ?",
                (portfolio_id, security_id, limit),
            ).fetchall()
        return [e for row in rows if (e := _evaluation(row[0])) is not None]

    @staticmethod
    def _deactivate(
        conn: sqlite3.Connection, portfolio_id: int, security_id: str
    ) -> None:
        conn.execute(
            "UPDATE position_theses SET active = 0 "
            "WHERE portfolio_id = ? AND security_id = ? AND active = 1",
            (portfolio_id, security_id),
        )

    @staticmethod
    def _latest(conn: sqlite3.Connection, portfolio_id: int, security_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM position_theses "
            "WHERE portfolio_id = ? AND security_id = ?",
            (portfolio_id, security_id),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _get(conn: sqlite3.Connection, thesis_id: int) -> PositionThesisV1 | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM position_theses WHERE id = ?", (thesis_id,)
        ).fetchone()
        return _row_to_thesis(row) if row else None
