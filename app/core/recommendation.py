"""Shared recommendation/actionability logic for watchlist ranking.

Kept deliberately small and dependency-light so both the analyst pipeline
and the web layer can import it without pulling in heavy modules.

TODO(#58): fold the watchlist template's inline recommendation thresholds
and AlertAgent's alert_trigger into this module so there is one source of
truth for "what counts as actionable".
"""

from __future__ import annotations

from app.schemas import StockRecord


def actionability_rank(record: StockRecord) -> int:
    """Rank a record by how tradable it is right now (higher = act sooner).

    Mirrors the recommendation buckets shown in the watchlist so the table
    surfaces Buy/breakout rows above merely high-scoring but non-actionable
    ones.
    """
    a = record.analysis
    if a is None:
        return 0
    if a.stage in ("Stage 3", "Stage 4") or a.score <= 3:
        return 0  # Sell / Avoid
    if a.stage == "Stage 2" and a.entry_zone == "broken_out" and a.score >= 7:
        return 5  # Buy (with or without volume confirmation)
    if a.stage == "Stage 2" and a.entry_zone == "approaching" and a.score >= 5:
        return 4  # Stay Alert
    if a.stage == "Stage 2" and a.entry_zone == "getting_close":
        return 3  # Watch
    if a.stage == "Stage 2" and a.entry_zone == "extended":
        return 2  # Extended
    return 1  # No action


def actionability_sort_key(record: StockRecord) -> tuple[int, int, int, int]:
    """Sort key ordering by actionability, then score, CANSLIM, momentum.

    Use with ``reverse=True`` so the most actionable, highest-quality setups
    appear first.
    """
    a = record.analysis
    return (
        actionability_rank(record),
        a.score if a else 0,
        a.canslim.total if a and a.canslim else 0,
        a.momentum.total if a and a.momentum else 0,
    )
