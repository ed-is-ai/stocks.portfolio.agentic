"""Repository for the append-only ``trade_annotations`` table (GH-17).

An annotation records the user's stated intent (and optional stop) for one
trade. Rows are only ever inserted: the newest row per trade is its current
annotation and earlier rows are its history. ``trades`` is never touched.

A trade correction deletes and re-inserts a ticker's trades under new ids,
so ``trade_id`` carries no foreign key (a cascade would silently delete the
notes; a plain key would block the correction). Each row also stores the
trade's fingerprint, and :func:`resolve_annotations` re-attaches a note
whose trade id no longer exists to the trade with the same fingerprint.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from app.core.ticker_identity import canonicalize_or_fallback, load_aliases
from app.repositories.db import Connect, session
from app.schemas import Trade
from app.schemas.trade_review import TradeAnnotationV1

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, portfolio_id, trade_id, intent, stated_stop, created_at, trade_fingerprint"
)


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def ticker_aliases() -> dict[str, str]:
    """Return the ticker aliases, or none when the alias file is unreadable."""
    try:
        return load_aliases()
    except Exception:
        logger.warning("trade annotations: aliases unreadable", exc_info=True)
        return {}


def trade_fingerprint(
    portfolio_id: int | None,
    ticker: str,
    action: str,
    date: str,
    shares: float,
    price: float,
    aliases: dict[str, str],
) -> str:
    """Return a trade's identity independent of its row id.

    The ticker is canonical, as a correction re-inserts it that way.
    """
    canonical = canonicalize_or_fallback(
        ticker.upper(), aliases, logger=logger, context="trade annotation"
    )
    return (
        f"{portfolio_id}|{canonical}|{action}|{date}|{float(shares)!r}|{float(price)!r}"
    )


def fingerprint_of(trade: Trade, aliases: dict[str, str]) -> str:
    """Return :func:`trade_fingerprint` for a ``Trade``."""
    return trade_fingerprint(
        trade.portfolio_id,
        trade.ticker,
        trade.action,
        trade.date,
        trade.shares,
        trade.price,
        aliases,
    )


def resolve_annotations(
    annotations: Iterable[TradeAnnotationV1], trades: Iterable[Trade]
) -> dict[int, list[TradeAnnotationV1]]:
    """Map each trade id to its annotations, newest (current) first.

    A note belongs to its ``trade_id`` while that trade exists; otherwise
    (the trade was corrected away) to the trade with the same fingerprint,
    re-keyed to that trade's id.
    """
    aliases = ticker_aliases()
    by_fingerprint = {
        fingerprint_of(t, aliases): t.id for t in trades if t.id is not None
    }
    live = set(by_fingerprint.values())
    resolved: dict[int, list[TradeAnnotationV1]] = {}
    for note in sorted(annotations, key=lambda a: a.id, reverse=True):
        trade_id = (
            note.trade_id
            if note.trade_id in live
            else by_fingerprint.get(note.trade_fingerprint)
        )
        if trade_id is None:
            continue
        if trade_id != note.trade_id:
            note = note.model_copy(update={"trade_id": trade_id})
        resolved.setdefault(trade_id, []).append(note)
    return resolved


def _row(row: tuple[Any, ...]) -> TradeAnnotationV1:
    return TradeAnnotationV1(
        id=int(row[0]),
        portfolio_id=int(row[1]),
        trade_id=int(row[2]),
        intent=str(row[3]),
        stated_stop=row[4],
        created_at=str(row[5]),
        trade_fingerprint=str(row[6] or ""),
    )


class TradeAnnotationsRepository:
    """Append and read trade annotations in ``trades.db``."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def add(
        self,
        portfolio_id: int,
        trade_id: int,
        intent: str,
        stated_stop: float | None,
        trade_fingerprint: str = "",
    ) -> TradeAnnotationV1:
        """Append one annotation row and return it."""
        created_at = _utc_now()
        with session(self._connect) as conn:
            cur = conn.execute(
                "INSERT INTO trade_annotations (portfolio_id, trade_id, intent, "
                "stated_stop, created_at, trade_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    portfolio_id,
                    trade_id,
                    intent,
                    stated_stop,
                    created_at,
                    trade_fingerprint,
                ),
            )
            row_id = cur.lastrowid
        assert row_id is not None
        return TradeAnnotationV1(
            id=row_id,
            portfolio_id=portfolio_id,
            trade_id=trade_id,
            intent=intent,
            stated_stop=stated_stop,
            created_at=created_at,
            trade_fingerprint=trade_fingerprint,
        )

    def all(self) -> list[TradeAnnotationV1]:
        """Return every annotation, oldest first."""
        with session(self._connect) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM trade_annotations ORDER BY id"
            ).fetchall()
        return [_row(r) for r in rows]

    def revision(self) -> tuple[int, int]:
        """Return (row count, max id): changes on every append."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trade_annotations"
            ).fetchone()
        return int(row[0]), int(row[1])
