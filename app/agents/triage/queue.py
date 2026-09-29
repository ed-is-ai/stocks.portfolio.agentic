"""Pure, deterministic risk-first attention queue builder (GH-18).

Groups typed source events into ``AttentionItemV1``s: one item per
``(portfolio, security or subject, category)``, ordered held risk, exits,
evidence, new setups. No I/O, no clock and no LLM: the same events in any
order always build the identical queue.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import groupby

from app.schemas.attention import (
    CATEGORY_ORDER,
    SEVERITY_ORDER,
    AttentionCategory,
    AttentionEventV1,
    AttentionItemV1,
    AttentionQueueV1,
    portfolio_key,
    run_key,
)
from app.schemas.evidence_ref import EvidenceRefV1

_PRIORITY: dict[AttentionCategory, str] = {
    "held_risk": "Risk to a held position, ranked first",
    "exit": "Exit signal on a held position",
    "evidence": "Evidence gap behind the findings",
    "new_setup": "New setup, ranked after held positions",
}
_REVIEW: dict[AttentionCategory, str] = {
    "held_risk": "Review the position against the risk policy.",
    "exit": "Review the exit signal against the thesis.",
    "evidence": "Review the evidence before relying on these findings.",
    "new_setup": "Review the setup on the watchlist.",
}


def build_attention_queue(
    events: Iterable[AttentionEventV1],
    *,
    portfolio_id: int | None,
    analysis_run_id: str | None,
    unavailable: Sequence[str],
) -> AttentionQueueV1:
    """Group ``events`` into a risk-first queue; order-independent and pure.

    Events sharing a ``source_event_id`` are kept once (the first in order).
    """
    unique: dict[str, AttentionEventV1] = {}
    for event in sorted(events, key=_event_key):
        unique.setdefault(event.source_event_id, event)
    ordered = list(unique.values())
    grouped = sorted(
        (list(group) for _, group in groupby(sorted(ordered, key=_group), _group)),
        key=lambda group: _event_key(group[0]),
    )
    return AttentionQueueV1(
        portfolio_id=portfolio_id,
        analysis_run_id=analysis_run_id,
        items=[_item(group, analysis_run_id) for group in grouped],
        events=ordered,
        unavailable=list(unavailable),
    )


def _subject(event: AttentionEventV1) -> str:
    """The security, else the source name of a source event, else the kind."""
    if event.security_id is not None:
        return event.security_id
    source = next((r.id for r in event.evidence if r.kind == "source_health"), None)
    return source or event.kind


def _group(event: AttentionEventV1) -> tuple[str, str, int]:
    """The grouping key: portfolio, subject and category."""
    return (
        str(event.portfolio_id),
        _subject(event),
        CATEGORY_ORDER.index(event.category),
    )


def _event_key(event: AttentionEventV1) -> tuple[int, int, bool, str, str, str, str]:
    """Category, severity, security (None last), kind, id, then content."""
    return (
        CATEGORY_ORDER.index(event.category),
        SEVERITY_ORDER.index(event.severity),
        event.security_id is None,
        event.security_id or "",
        event.kind,
        event.source_event_id,
        event.model_dump_json(),
    )


def _item(group: list[AttentionEventV1], run_id: str | None) -> AttentionItemV1:
    """One item from its already-ordered events; the first fresh one leads,
    so the title reads "Stale:" only when every event is stale."""
    lead = next((e for e in group if not e.stale), group[0])
    evidence: list[EvidenceRefV1] = []
    for event in group:
        evidence += [ref for ref in event.evidence if ref not in evidence]
    observed = [e.observed_at for e in group if e.observed_at is not None]
    extra = len(group) - 1
    return AttentionItemV1(
        id=(
            f"attention:{run_key(run_id)}:{portfolio_key(lead.portfolio_id)}:"
            f"{lead.category}:{_subject(lead)}"
        ),
        severity=min((e.severity for e in group), key=SEVERITY_ORDER.index),
        kind=lead.category,
        portfolio_id=lead.portfolio_id,
        security_id=lead.security_id,
        title=f"{lead.title} (+{extra} more)" if extra else lead.title,
        summary=_summary(group),
        evidence=evidence,
        source_event_ids=[e.source_event_id for e in group],
        raised_by=", ".join(sorted({e.raised_by for e in group})),
        observed_at=min(observed, default=None),
    )


def _summary(group: list[AttentionEventV1]) -> str:
    """Why the item ranks where it does, its evidence date and what to review."""
    category = group[0].category
    count = f"{len(group)} signal{'s' if len(group) != 1 else ''}"
    fresh = [e for e in group if not e.stale]
    dates = [e.as_of for e in fresh or group if e.as_of is not None]
    as_of = min(dates).isoformat() if dates else "an unknown date"
    if not fresh:
        freshness = f"Evidence is stale (as of {as_of})."
    elif len(fresh) < len(group):
        freshness = f"Evidence as of {as_of} (includes stale evidence)."
    else:
        freshness = f"Evidence as of {as_of}."
    return f"{_PRIORITY[category]} · {count}. {freshness} {_REVIEW[category]}"
