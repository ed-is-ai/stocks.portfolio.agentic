"""Typed contract for the risk-first attention queue (GH-18).

``AttentionItemV1`` is the frozen item shape later surfaces render; the
queue wraps the ordered items with every original ``AttentionEventV1`` so
nothing a source raised is ever dropped, only grouped.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.evidence_ref import EvidenceRefV1

AttentionCategory = Literal["held_risk", "exit", "evidence", "new_setup"]
AttentionSeverity = Literal["high", "medium", "info"]

#: Risk-first display order; each category's fixed severity.
CATEGORY_SEVERITY: dict[AttentionCategory, AttentionSeverity] = {
    "held_risk": "high",
    "exit": "high",
    "evidence": "medium",
    "new_setup": "info",
}
CATEGORY_ORDER: tuple[AttentionCategory, ...] = tuple(CATEGORY_SEVERITY)
SEVERITY_ORDER: tuple[AttentionSeverity, ...] = ("high", "medium", "info")


def run_key(run_id: str | None) -> str:
    """``run_id`` for a stable id; a fixed token when the run is unknown."""
    return run_id or "unknown-run"


def portfolio_key(portfolio_id: int | None) -> str:
    """``portfolio_id`` for a stable id; a fixed token when run-wide."""
    return "all-portfolios" if portfolio_id is None else str(portfolio_id)


class _FrozenAttentionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence: list[EvidenceRefV1]
    observed_at: datetime | None

    @property
    def as_of(self) -> date | None:
        """The evidence date: ``observed_at``, else the oldest evidence date."""
        if self.observed_at is not None:
            return self.observed_at.date()
        return min((ref.as_of for ref in self.evidence if ref.as_of), default=None)


class AttentionEventV1(_FrozenAttentionModel):
    """One original finding from one source; ``None`` ids mean run-wide."""

    source_event_id: str
    category: AttentionCategory
    severity: AttentionSeverity
    kind: str
    portfolio_id: int | None
    security_id: str | None
    title: str
    summary: str
    raised_by: str
    stale: bool = False


class AttentionItemV1(_FrozenAttentionModel):
    """One grouped queue entry; ``kind`` is its category."""

    id: str
    severity: AttentionSeverity
    kind: AttentionCategory
    portfolio_id: int | None
    security_id: str | None
    title: str
    summary: str
    source_event_ids: list[str]
    raised_by: str


class AttentionQueueV1(BaseModel):
    """The ordered items plus every original event and the missing sources."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    portfolio_id: int | None
    analysis_run_id: str | None
    items: list[AttentionItemV1]
    events: list[AttentionEventV1]
    unavailable: list[str]

    @property
    def urgent_count(self) -> int:
        """Items about this portfolio, high or medium, with fresh evidence.

        Run-wide items (source health, freshness), items whose every event
        is stale and notification items (GH-21, reviewed on the AI Desk) are
        listed but not counted.
        """
        return sum(
            item.portfolio_id is not None
            and item.severity != "info"
            and not any(ref.kind == "notification" for ref in item.evidence)
            and not all(event.stale for event in self.events_for(item))
            for item in self.items
        )

    def events_for(self, item: AttentionItemV1) -> list[AttentionEventV1]:
        """The original events grouped into ``item``, in queue order."""
        ids = set(item.source_event_ids)
        return [event for event in self.events if event.source_event_id in ids]
