"""Pure, deterministic risk-first attention queue builder (GH-18).

Groups typed source events into ``AttentionItemV1``s: one item per
``(portfolio, security or subject, category)``, ordered held risk, exits,
evidence, new setups. A rule that fires on two or more holdings of one
portfolio (``ROLLUP_KINDS``) becomes one cross-holding rollup item keyed
``rollup:<category>:<rule>:<portfolio>`` (GH-128). No I/O, no clock and no
LLM: the same events in any order always build the identical queue.
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

#: Per-holding rules rolled up into one item, with the rollup's title.
ROLLUP_KINDS: dict[str, str] = {
    "no_stop": "{n} holdings have no evidenced stop",
    "exit_evidence_gap": "{n} holdings lack exit evidence",
}

RollupKey = tuple[int | None, AttentionCategory, str]


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
    rollups = _rollups(ordered)
    rolled = {e.source_event_id for group in rollups.values() for e in group}
    rest = [e for e in ordered if e.source_event_id not in rolled]
    keyed = [
        (_event_key(group[0]), _item(group, analysis_run_id))
        for group in (list(g) for _, g in groupby(sorted(rest, key=_group), _group))
    ] + [
        (_rollup_sort_key(key, group), _rollup_item(key, group))
        for key, group in rollups.items()
    ]
    return AttentionQueueV1(
        portfolio_id=portfolio_id,
        analysis_run_id=analysis_run_id,
        items=[item for _, item in sorted(keyed, key=lambda pair: pair[0])],
        events=ordered,
        unavailable=list(unavailable),
    )


def rollup_id(category: AttentionCategory, kind: str, portfolio_id: int | None) -> str:
    """The stable, run-independent id of a cross-holding rollup item."""
    return f"rollup:{category}:{kind}:{portfolio_key(portfolio_id)}"


def _rollups(
    events: Sequence[AttentionEventV1],
) -> dict[RollupKey, list[AttentionEventV1]]:
    """Events of a ``ROLLUP_KINDS`` rule, per portfolio, where the rule
    fires on two or more securities; in queue order."""
    buckets: dict[RollupKey, list[AttentionEventV1]] = {}
    for event in events:
        if event.kind in ROLLUP_KINDS and event.security_id is not None:
            key = (event.portfolio_id, event.category, event.kind)
            buckets.setdefault(key, []).append(event)
    return {
        key: group
        for key, group in buckets.items()
        if len({e.security_id for e in group}) > 1
    }


def _rollup_sort_key(
    key: RollupKey, group: list[AttentionEventV1]
) -> tuple[int, int, bool, str, str, str, str]:
    """Rank a rollup like a run-wide item of its category and rule."""
    _, category, kind = key
    return (
        CATEGORY_ORDER.index(category),
        SEVERITY_ORDER.index(group[0].severity),
        True,
        "",
        kind,
        rollup_id(category, kind, key[0]),
        "",
    )


def _rollup_item(key: RollupKey, group: list[AttentionEventV1]) -> AttentionItemV1:
    """One item for a rule firing on several holdings; the per-holding
    item it replaces is kept in its events."""
    portfolio_id, category, kind = key
    base = _item(group, None)
    securities = sorted({e.security_id or "" for e in group})
    return base.model_copy(
        update={
            "id": rollup_id(category, kind, portfolio_id),
            "security_id": None,
            "title": ROLLUP_KINDS[kind].format(n=len(securities)),
            "summary": f"{', '.join(securities)}. {base.summary}",
        }
    )


def _subject(event: AttentionEventV1) -> str:
    """The notification (each is its own item, GH-21), else the security,
    else the source name of a source event, else the kind."""
    if any(ref.kind == "notification" for ref in event.evidence):
        return event.source_event_id
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
