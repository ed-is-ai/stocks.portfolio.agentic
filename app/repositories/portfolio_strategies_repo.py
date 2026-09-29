"""Repository for the ``portfolio_strategies`` table in ``trades.db`` (#440).

At most one Strategy assignment exists per portfolio — enforced by the
``portfolio_id`` PRIMARY KEY. ``upsert`` replaces the row in place so
``assigned_at`` records the first assignment and ``updated_at`` advances on
every write. Each upsert and clear also appends one row to the
append-only ``portfolio_strategy_history`` in the same transaction (GH-17),
so a trade review can tell which Strategy covered a past date.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.repositories.db import Connect, session
from app.schemas.strategy_assignment import StrategyAssignment
from app.services.backtest.strategy_protocol import JsonScalar


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _canonical_parameters(parameters: Mapping[str, JsonScalar]) -> str:
    """Serialise parameters deterministically (sorted keys, compact)."""
    return json.dumps(dict(parameters), sort_keys=True, separators=(",", ":"))


def _row_to_assignment(row: tuple[Any, ...]) -> StrategyAssignment:
    try:
        parameters: dict[str, JsonScalar] = json.loads(row[2])
    except (json.JSONDecodeError, TypeError):
        # A corrupt snapshot must never take down portfolio rendering or
        # the #442 dispatch loop — degrade to an empty parameter set.
        parameters = {}
    return StrategyAssignment(
        portfolio_id=int(row[0]),
        strategy_id=str(row[1]),
        parameters=parameters,
        assigned_at=str(row[3]),
        updated_at=str(row[4]),
    )


@dataclass(frozen=True)
class StrategyHistoryEntry:
    """One append-only assignment change; ``strategy_id`` None is a clear."""

    id: int
    strategy_id: str | None
    recorded_at: str


def _append_history(
    conn: sqlite3.Connection,
    portfolio_id: int,
    strategy_id: str | None,
    parameters_json: str,
    recorded_at: str,
) -> None:
    """Append one history row on the caller's connection (same transaction)."""
    conn.execute(
        "INSERT INTO portfolio_strategy_history "
        "(portfolio_id, strategy_id, parameters_json, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        (portfolio_id, strategy_id, parameters_json, recorded_at),
    )


_ASSIGNMENT_COLUMNS = (
    "portfolio_id, strategy_id, parameters_json, assigned_at, updated_at"
)


class PortfolioStrategiesRepository:
    """Typed CRUD access to the ``portfolio_strategies`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get(self, portfolio_id: int) -> StrategyAssignment | None:
        """Return the portfolio's assignment, or None if it has none."""
        with session(self._connect) as conn:
            row = conn.execute(
                f"SELECT {_ASSIGNMENT_COLUMNS} FROM portfolio_strategies "
                "WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()
        return _row_to_assignment(row) if row else None

    def upsert(
        self,
        portfolio_id: int,
        strategy_id: str,
        parameters: Mapping[str, JsonScalar],
    ) -> StrategyAssignment:
        """Insert or replace the portfolio's single assignment row.

        ``assigned_at`` is set only on first insert and preserved across
        replaces; ``updated_at`` advances on every write. The stored
        parameter snapshot is canonical (sorted keys, compact separators).
        """
        now = _utc_now()
        parameters_json = _canonical_parameters(parameters)
        with session(self._connect) as conn:
            conn.execute(
                "INSERT INTO portfolio_strategies "
                f"({_ASSIGNMENT_COLUMNS}) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(portfolio_id) DO UPDATE SET "
                "strategy_id = excluded.strategy_id, "
                "parameters_json = excluded.parameters_json, "
                "updated_at = excluded.updated_at",
                (
                    portfolio_id,
                    strategy_id,
                    parameters_json,
                    now,
                    now,
                ),
            )
            _append_history(conn, portfolio_id, strategy_id, parameters_json, now)
            row = conn.execute(
                f"SELECT {_ASSIGNMENT_COLUMNS} FROM portfolio_strategies "
                "WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()
        if row is None:  # pragma: no cover — the upsert above just wrote it
            raise RuntimeError(
                f"portfolio_strategies row for portfolio {portfolio_id} "
                "disappeared after upsert"
            )
        return _row_to_assignment(row)

    def clear(self, portfolio_id: int) -> bool:
        """Remove the portfolio's assignment. True if a row was deleted.

        A clear that removed an assignment appends one history row
        (``strategy_id`` NULL) in the same transaction, so "no Strategy from
        here on" is recorded too; clearing nothing records nothing.
        """
        with session(self._connect) as conn:
            cur = conn.execute(
                "DELETE FROM portfolio_strategies WHERE portfolio_id = ?",
                (portfolio_id,),
            )
            if cur.rowcount <= 0:
                return False
            _append_history(conn, portfolio_id, None, "{}", _utc_now())
            return True

    def history(self, portfolio_id: int) -> list[StrategyHistoryEntry]:
        """Return the portfolio's append-only assignment history, oldest first."""
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT id, strategy_id, recorded_at FROM portfolio_strategy_history "
                "WHERE portfolio_id = ? ORDER BY recorded_at, id",
                (portfolio_id,),
            ).fetchall()
        return [StrategyHistoryEntry(int(r[0]), r[1], str(r[2])) for r in rows]

    def history_revision(self) -> tuple[int, int]:
        """Return (row count, max id): changes whenever history is appended."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM portfolio_strategy_history"
            ).fetchone()
        return int(row[0]), int(row[1])

    def list_assigned(self) -> list[StrategyAssignment]:
        """Return every assignment, ordered by portfolio id.

        This is the stable contract the per-portfolio daily-email dispatch
        loop (#442) consumes — keep the return shape unchanged.
        """
        with session(self._connect) as conn:
            rows = conn.execute(
                f"SELECT {_ASSIGNMENT_COLUMNS} FROM portfolio_strategies "
                "ORDER BY portfolio_id"
            ).fetchall()
        return [_row_to_assignment(row) for row in rows]
