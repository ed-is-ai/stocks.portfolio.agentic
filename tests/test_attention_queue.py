"""Unit tests for the pure risk-first attention queue builder (GH-18)."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations

from app.agents.triage.queue import build_attention_queue
from app.agents.triage.sources import setup_events
from app.schemas.attention import AttentionEventV1, AttentionQueueV1
from app.schemas.evidence_ref import EvidenceRefV1

AT = datetime(2026, 9, 28, 21, tzinfo=UTC)


def _event(
    source_event_id: str,
    category: str,
    *,
    kind: str = "k",
    security_id: str | None = None,
    portfolio_id: int | None = 1,
    stale: bool = False,
    evidence_kind: str = "fact",
    evidence_id: str = "e",
) -> AttentionEventV1:
    severity = {"held_risk": "high", "exit": "high", "evidence": "medium"}
    return AttentionEventV1.model_validate(
        {
            "source_event_id": source_event_id,
            "category": category,
            "severity": severity.get(category, "info"),
            "kind": kind,
            "portfolio_id": portfolio_id,
            "security_id": security_id,
            "title": f"{source_event_id} title",
            "summary": f"{source_event_id} summary",
            "evidence": [
                EvidenceRefV1(kind=evidence_kind, id=evidence_id, source="test")
            ],
            "raised_by": "test",
            "observed_at": AT,
            "stale": stale,
        }
    )


SETUP = _event("setup", "new_setup", kind="breakout", security_id="NEW")
SOURCE = _event(
    "source",
    "evidence",
    kind="source_failed",
    portfolio_id=None,
    evidence_kind="source_health",
    evidence_id="finviz",
)
SELL = _event("sell", "exit", kind="strategy_sell", security_id="AAA")
THESIS = _event("thesis", "exit", kind="thesis_invalidated", security_id="AAA")
CONCENTRATION = _event(
    "concentration", "held_risk", kind="position_concentration", security_id="AAA"
)
MIXED = [SETUP, SOURCE, SELL, THESIS, CONCENTRATION]


def _queue(events: list[AttentionEventV1]) -> AttentionQueueV1:
    return build_attention_queue(
        events, portfolio_id=1, analysis_run_id="run-1", unavailable=()
    )


def test_risk_first_order() -> None:
    queue = _queue([SETUP, SOURCE, SELL, CONCENTRATION])

    assert [item.kind for item in queue.items] == [
        "held_risk",
        "exit",
        "evidence",
        "new_setup",
    ]
    assert [item.severity for item in queue.items] == [
        "high",
        "high",
        "medium",
        "info",
    ]
    # The run-wide source failure is listed but not counted.
    assert queue.urgent_count == 2


def test_sell_and_invalidated_thesis_group_into_one_exit_item() -> None:
    queue = _queue([SELL, THESIS])

    (item,) = queue.items
    assert item.kind == "exit"
    assert item.security_id == "AAA"
    assert item.source_event_ids == ["sell", "thesis"]
    assert queue.events_for(item) == [SELL, THESIS]
    assert item.title == "sell title (+1 more)"
    assert "2 signals" in item.summary


def test_same_events_in_any_order_build_the_identical_queue() -> None:
    expected = _queue(MIXED)

    for order in permutations(MIXED):
        assert _queue(list(order)) == expected


def test_building_twice_is_idempotent_and_ids_are_stable() -> None:
    first, second = _queue(MIXED), _queue(MIXED)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.items[0].id == "attention:run-1:1:held_risk:AAA"
    assert [i.id for i in first.items] == [i.id for i in second.items]


def test_every_source_event_is_in_exactly_one_item_and_in_events() -> None:
    queue = _queue(MIXED)

    grouped = [sid for item in queue.items for sid in item.source_event_ids]
    ids = sorted(e.source_event_id for e in MIXED)
    assert sorted(grouped) == ids
    assert sorted(e.source_event_id for e in queue.events) == ids


def test_portfolio_wide_events_group_by_kind_and_sources_by_name() -> None:
    capital = _event("capital", "held_risk", kind="capital_at_risk")
    sector_a = _event("sector-a", "held_risk", kind="sector_concentration")
    sector_b = _event("sector-b", "held_risk", kind="sector_concentration")
    other_source = SOURCE.model_copy(
        update={
            "source_event_id": "source-2",
            "evidence": [
                EvidenceRefV1(kind="source_health", id="congress", source="test")
            ],
        }
    )
    queue = _queue([capital, sector_a, sector_b, SOURCE, other_source])

    assert [item.source_event_ids for item in queue.items] == [
        ["capital"],
        ["sector-a", "sector-b"],
        ["source"],
        ["source-2"],
    ]


def test_the_same_security_in_two_portfolios_is_two_items() -> None:
    other = SELL.model_copy(update={"source_event_id": "sell-2", "portfolio_id": 2})

    assert len(_queue([SELL, other]).items) == 2


def test_summary_says_why_when_and_what_to_review() -> None:
    (item,) = _queue([CONCENTRATION]).items

    assert item.summary == (
        "Risk to a held position, ranked first · 1 signal. "
        "Evidence as of 2026-09-28. Review the position against the risk policy."
    )
    for sizing in ("buy ", "sell ", "shares", "%", "£"):
        assert sizing not in item.summary.lower()


def test_stale_evidence_is_said_in_the_summary() -> None:
    (item,) = _queue([THESIS.model_copy(update={"stale": True})]).items

    assert "Evidence is stale (as of 2026-09-28)." in item.summary


def test_empty_queue_carries_unavailable_sources() -> None:
    queue = build_attention_queue(
        [], portfolio_id=None, analysis_run_id=None, unavailable=("Risk unavailable",)
    )

    assert queue.items == []
    assert queue.events == []
    assert queue.urgent_count == 0
    assert queue.unavailable == ["Risk unavailable"]


def test_run_wide_and_stale_only_items_are_listed_but_not_counted() -> None:
    stale_thesis = THESIS.model_copy(update={"stale": True})

    queue = _queue([SOURCE, stale_thesis])

    assert [item.kind for item in queue.items] == ["exit", "evidence"]
    assert queue.urgent_count == 0
    # A fresh event about this portfolio makes the same exit item count.
    assert _queue([SOURCE, stale_thesis, SELL]).urgent_count == 1


def test_a_fresh_sell_with_a_stale_thesis_is_not_described_stale() -> None:
    earlier = datetime(2026, 9, 1, tzinfo=UTC)
    # Its kind sorts first, so a stale event would otherwise lead the item.
    stale = _event("thesis", "exit", kind="a_thesis", security_id="AAA", stale=True)
    stale = stale.model_copy(
        update={"title": "Stale: AAA: thesis invalidated", "observed_at": earlier}
    )

    (item,) = _queue([SELL, stale]).items

    assert item.title == "sell title (+1 more)"
    assert "Evidence as of 2026-09-28 (includes stale evidence)." in item.summary
    assert "Evidence is stale" not in item.summary
    (only_stale,) = _queue([stale]).items
    assert only_stale.title == "Stale: AAA: thesis invalidated"
    assert "Evidence is stale (as of 2026-09-01)." in only_stale.summary


def test_duplicate_source_events_are_kept_once_and_ids_never_say_none() -> None:
    entry = ("NEW", "entry_triggered", "Entry triggered")
    events = setup_events([entry, entry], run_id=None, observed_at=None)

    queue = build_attention_queue(
        events, portfolio_id=None, analysis_run_id=None, unavailable=()
    )

    (item,) = queue.items
    assert len(queue.events) == 1
    assert item.title == "NEW: Entry triggered"
    assert item.id == "attention:unknown-run:all-portfolios:new_setup:NEW"
    assert item.source_event_ids == ["setup:unknown-run:NEW:entry_triggered"]
