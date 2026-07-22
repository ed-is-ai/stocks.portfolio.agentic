"""Shared recommendation/actionability logic for watchlist ranking.

Kept deliberately small and dependency-light so both the analyst pipeline
and the web layer can import it without pulling in heavy modules.

``classify_recommendation`` is the single source of truth for "what should
the user do about this row" — the watchlist template's Rec column, its
filter buttons, and (via ``app.core.alerting``) the AlertAgent's email
triggers all derive from it, so the screen and inbox can no longer disagree
about the underlying thresholds (#58).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas import StockRecord

#: Stage label for a confirmed uptrend (Weinstein Stage 2 / Minervini SEPA).
STAGE_2 = "Stage 2"
#: Minimum score for a broken-out Stage 2 setup to count as a "Buy".
BUY_SCORE_MIN = 7
#: Minimum score for an approaching Stage 2 setup to count as "Stay Alert".
STAY_ALERT_SCORE_MIN = 5


@dataclass(frozen=True)
class Recommendation:
    """Canonical recommendation bucket for one watchlist row.

    Attributes:
        bucket: fine-grained bucket — one of ``no_analysis``, ``sell``,
            ``avoid``, ``buy_lowvol``, ``buy``, ``extended``, ``alert``,
            ``watch``, ``none``.
        text: display text for the Rec column (e.g. "Buy", "Stay Alert").
        css_class: CSS class for the Rec column badge.
        filter_key: coarse key the on-page filter buttons group by
            (``"alert"``, ``"buy"``, or ``"other"``).
    """

    bucket: str
    text: str
    css_class: str
    filter_key: str


_NO_ANALYSIS = Recommendation("no_analysis", "", "rec-none", "other")
_NONE = Recommendation("none", "No action", "rec-none", "other")

#: Rank each bucket by how tradable it is right now (higher = act sooner).
#: Missing analysis ranks alongside Sell/Avoid at the bottom.
_BUCKET_RANK = {
    "no_analysis": 0,
    "sell": 0,
    "avoid": 0,
    "none": 1,
    "extended": 2,
    "watch": 3,
    "alert": 4,
    "buy_lowvol": 5,
    "buy": 5,
}


def classify_recommendation(
    record: StockRecord, *, is_portfolio_holding: bool = False
) -> Recommendation:
    """Classify ``record`` into the canonical recommendation bucket.

    ``is_portfolio_holding`` only affects the Sell/Avoid text (a held
    position that has broken down should say "Sell", not "Avoid").
    """
    a = record.analysis
    if a is None:
        return _NO_ANALYSIS

    if a.stage in ("Stage 3", "Stage 4") or a.score <= 3:
        if is_portfolio_holding:
            return Recommendation("sell", "Sell", "rec-sell", "other")
        return Recommendation("avoid", "Avoid", "rec-avoid", "other")

    if a.stage == STAGE_2 and a.entry_zone == "broken_out" and a.score >= BUY_SCORE_MIN:
        if not a.volume_confirmed:
            return Recommendation("buy_lowvol", "Buy · low vol", "rec-lowvol", "buy")
        return Recommendation("buy", "Buy", "rec-buy", "buy")

    if a.stage == STAGE_2 and a.entry_zone == "extended":
        return Recommendation("extended", "Extended", "rec-extended", "other")

    if (
        a.stage == STAGE_2
        and a.entry_zone == "approaching"
        and a.score >= STAY_ALERT_SCORE_MIN
    ):
        return Recommendation("alert", "Stay Alert", "rec-alert", "alert")

    if a.stage == STAGE_2 and a.entry_zone == "getting_close":
        return Recommendation("watch", "Watch", "rec-watch", "other")

    return _NONE


def actionability_rank(record: StockRecord) -> int:
    """Rank a record by how tradable it is right now (higher = act sooner).

    Mirrors the recommendation buckets shown in the watchlist so the table
    surfaces Buy/breakout rows above merely high-scoring but non-actionable
    ones.
    """
    return _BUCKET_RANK[classify_recommendation(record).bucket]


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
