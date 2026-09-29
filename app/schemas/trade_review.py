"""Trade process review schemas (GH-17).

A review judges whether a trade followed a sound process, never how it
turned out: each check reads only evidence dated before the trade and
cites it. Annotations are the user's own stated intent for a trade; the
weekly facts are deterministic counts, and the interpretation is Claude's
optional reading of those anonymised facts.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.evidence_ref import EvidenceRefV1

CheckKind = Literal[
    "valid_setup",
    "entry_location",
    "evidenced_stop",
    "exit_signal",
    "strategy_alignment",
    "data_completeness",
]
CheckStatus = Literal["followed", "deviated", "unknown", "n_a"]
CHECK_STATUSES: tuple[CheckStatus, ...] = ("followed", "deviated", "unknown", "n_a")
MAX_INTENT_LENGTH = 1000

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ReviewRuleV1(BaseModel):
    """A checklist rule: a short stable id plus its plain wording."""

    model_config = _FROZEN

    id: str
    wording: str


class TradeCheckV1(BaseModel):
    """One check's verdict, the evidence it read and how it was reached."""

    model_config = _FROZEN

    kind: CheckKind
    status: CheckStatus
    rule: ReviewRuleV1
    note: str = ""
    evidence: tuple[EvidenceRefV1, ...] = ()
    observed: dict[str, float | str | None] = Field(default_factory=dict)
    calculation: str


class TradeReviewV1(BaseModel):
    """Every check for one BUY or SELL trade."""

    model_config = _FROZEN

    trade_id: int
    portfolio_id: int
    ticker: str
    action: Literal["BUY", "SELL"]
    trade_date: date
    checks: tuple[TradeCheckV1, ...]
    #: A position held before tracking began; kept out of recurring
    #: deviations, since it had no entry the process could shape.
    opening_lot: bool = False

    @property
    def week(self) -> str:
        """Return the trade date's ISO week, e.g. ``2026-W07``."""
        return iso_week(self.trade_date)

    def check(self, kind: CheckKind) -> TradeCheckV1 | None:
        """Return this review's check of ``kind``, if it has one."""
        return next((c for c in self.checks if c.kind == kind), None)


class TradeAnnotationV1(BaseModel):
    """One append-only note of the user's intent for a trade."""

    model_config = _FROZEN

    id: int
    portfolio_id: int
    trade_id: int
    intent: Annotated[str, Field(max_length=MAX_INTENT_LENGTH)]
    stated_stop: Annotated[float, Field(gt=0)] | None = None
    created_at: str
    #: The annotated trade's identity (portfolio, ticker, action, date,
    #: shares, price), so the note survives a correction that re-inserts
    #: the trade under a new id; empty when unknown.
    trade_fingerprint: str = ""


class RecurringDeviationV1(BaseModel):
    """A rule deviated at least twice in one week."""

    model_config = _FROZEN

    rule_id: str
    wording: str
    count: int


class WeeklyFactsV1(BaseModel):
    """Deterministic counts for one ISO week's reviewed trades."""

    model_config = _FROZEN

    week: str
    trade_ids: tuple[int, ...]
    counts: dict[str, dict[str, int]]
    recurring: tuple[RecurringDeviationV1, ...]


class TradeReviewInterpretationV1(BaseModel):
    """Claude's reading of one week's anonymised facts; never stored."""

    model_config = _FROZEN

    summary: Annotated[str, Field(min_length=1)]
    patterns: tuple[str, ...] = ()


def iso_week(day: date) -> str:
    """Return ``day``'s ISO week label, e.g. ``2026-W07``."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"
