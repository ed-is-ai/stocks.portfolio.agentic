"""Unit tests for the pure attention-event mappers (GH-18)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.agents.thesis.evaluator import evaluate_thesis
from app.agents.triage.queue import build_attention_queue
from app.agents.triage.sources import (
    freshness_events,
    held_events,
    held_hit,
    notification_events,
    record_setups,
    recommendation_events,
    risk_events,
    setup_events,
    source_health_events,
    thesis_events,
)
from app.schemas.evidence_ref import EvidenceRefV1
from app.schemas.notification import (
    Notification,
    NotificationCategory,
    NotificationSeverity,
)
from app.schemas.position_thesis import ThesisSummary
from app.schemas.source_health import SourceHealth, SourceName, SourceState
from app.services.freshness_service import Freshness, FreshnessState
from tests.test_alert_digest_held import _buy_record
from tests.test_alert_digest_held import _position as _held
from tests.test_portfolio_agent_view import (
    POSITIONS,
    _diagnostic,
    _finding,
    _result,
    _risk,
)
from tests.test_thesis_evaluator import FRESH, META, make_record, make_thesis

AT = datetime(2026, 9, 28, 21, tzinfo=UTC)


def test_only_high_risk_findings_become_held_risk_events() -> None:
    report = _risk(
        _finding("position_concentration", "high", "BBB is 40%", "BBB"),
        _finding("capital_at_risk", "high", "Capital at risk is 9%", "AAA", "BBB"),
        _finding("no_stop", "medium", "AAA has no stop", "AAA"),
    )

    events = risk_events(
        report, portfolio_id=1, positions=POSITIONS, prices_as_of="2026-09-28 20:00"
    )

    assert [(e.category, e.kind, e.security_id) for e in events] == [
        ("held_risk", "position_concentration", "0P0.L"),
        ("held_risk", "capital_at_risk", None),
    ]
    assert events[0].as_of == date(2026, 9, 28)
    assert events[0].source_event_id == "risk:1:position_concentration:BBB"


def test_sells_and_held_exit_gaps_follow_the_recommendation() -> None:
    result = _result(_diagnostic("0P0.L", 166), _diagnostic("ZZZ", 10))

    events = recommendation_events(result, positions=POSITIONS)

    assert [(e.category, e.title) for e in events] == [
        ("exit", "AAA: Strategy says Sell"),
        ("evidence", "BBB: exit evidence gap (166 / 200)"),
    ]
    assert not any(e.stale for e in events)
    assert events[0].observed_at == result.generated_at


def test_a_stale_artifact_marks_sells_stale() -> None:
    result = _result().model_copy(update={"freshness": "stale"})

    (sell,) = recommendation_events(result, positions=POSITIONS)

    assert sell.stale
    assert sell.title == "Stale: AAA: Strategy says Sell"


def _invalidated() -> ThesisSummary:
    active = make_thesis({"kind": "close_below_sma", "period": 50})
    latest = evaluate_thesis(active, make_record(price=94.0), META, FRESH)
    return ThesisSummary(active=active, latest=latest, current=True)


def test_current_invalidated_thesis_is_a_fresh_exit_event() -> None:
    (event,) = thesis_events(
        {"AAA": _invalidated()}, portfolio_id=1, positions=POSITIONS, observed_at=AT
    )

    assert (event.category, event.title, event.stale) == (
        "exit",
        "AAA: thesis invalidated",
        False,
    )
    assert event.observed_at == AT


def test_earlier_run_thesis_is_stale_and_never_fresh() -> None:
    earlier = ThesisSummary(active=_invalidated().active, latest=_invalidated().latest)

    (event,) = thesis_events(
        {"AAA": earlier}, portfolio_id=1, positions=POSITIONS, observed_at=AT
    )

    assert event.stale
    assert event.title == "Stale: AAA: thesis invalidated"
    assert event.observed_at is None


def _health(source: SourceName, state: SourceState, **extra: object) -> SourceHealth:
    return SourceHealth.model_validate(
        {"source": source, "state": state, "completed_at": AT, **extra}
    )


def test_only_non_ok_sources_are_evidence_events() -> None:
    health = {
        SourceName.CONGRESS: _health(SourceName.CONGRESS, SourceState.OK),
        SourceName.STOCKTWITS: _health(
            SourceName.STOCKTWITS, SourceState.FAILED, display_message="HTTP 500"
        ),
        SourceName.WHALE_WISDOM: _health(
            SourceName.WHALE_WISDOM, SourceState.SKIPPED, data_as_of=date(2026, 9, 1)
        ),
    }

    events = source_health_events(health, run_id="run-1")

    assert [(e.kind, e.summary, e.stale) for e in events] == [
        ("source_failed", "HTTP 500", False),
        (
            "source_skipped",
            "WhaleWisdom returned no usable data. Evidence is stale.",
            True,
        ),
    ]
    assert events[0].source_event_id == "source:run-1:stocktwits"


def test_stale_artifact_adds_one_evidence_item_and_stale_setups() -> None:
    stale = Freshness(state=FreshnessState.STALE, refreshed_at=AT)
    setups = setup_events(
        [("NEW", "breakout", "VCP Breakout")],
        run_id="run-1",
        observed_at=AT,
        stale=True,
    )

    queue = build_attention_queue(
        [*freshness_events(stale, run_id="run-1"), *setups],
        portfolio_id=1,
        analysis_run_id="run-1",
        unavailable=(),
    )

    assert [i.title for i in queue.items] == [
        "Analysis is stale (as of 2026-09-28)",
        "Stale: NEW: VCP Breakout",
    ]
    assert freshness_events(Freshness(state=FreshnessState.FRESH), run_id="r") == []
    unknown = freshness_events(Freshness(state=FreshnessState.UNKNOWN), run_id="r")
    assert unknown[0].title == "Analysis freshness is unknown"


def test_record_setups_are_unheld_breakouts_only() -> None:
    records = [
        _buy_record("NEW", breakout=True),
        _buy_record("AAA", breakout=True),
        _buy_record("WAIT", breakout=False),
    ]

    assert record_setups(records, ["AAA"]) == [("NEW", "breakout", "VCP Breakout")]


def test_held_hit_matches_the_stop_and_target_predicate() -> None:
    assert held_hit(_held("A", 80.0, stop_loss=90.0, profit_target_20=70.0)) == "stop"
    assert held_hit(_held("A", 130.0, profit_target_20=120.0)) == "target"
    assert held_hit(_held("A", 100.0, stop_loss=90.0, profit_target_20=120.0)) is None
    assert (
        held_hit(_held("A", 100.0).model_copy(update={"current_price": None})) is None
    )


def test_held_events_and_ids_do_not_depend_on_the_clock() -> None:
    positions = [
        _held("AAA", 80.0, stop_loss=90.0),
        _held("BBB", 130.0, profit_target_20=120.0),
    ]

    def _events(at: datetime) -> list[tuple[str, str, str]]:
        return [
            (e.category, e.title, e.source_event_id)
            for e in held_events(
                positions,
                portfolio_id=2,
                portfolio_name="ISA",
                run_id="run-1",
                observed_at=at,
            )
        ]

    assert _events(AT) == [
        ("held_risk", "ISA: AAA at or below its stop", "held:run-1:2:AAA:stop"),
        ("exit", "ISA: BBB reached its profit target", "held:run-1:2:BBB:target"),
    ]
    assert _events(datetime(2027, 1, 1, tzinfo=UTC)) == _events(AT)


def test_held_events_take_this_runs_trailing_and_watched_stop_signals() -> None:
    positions = [
        _held("AAA", 95.0),
        _held("BBB", 80.0, stop_loss=90.0),
        _held("CCC", 70.0),
        _held("DDD", 100.0),
    ]

    events = held_events(
        positions,
        portfolio_id=None,
        portfolio_name="ISA",
        run_id=None,
        observed_at=None,
        signals={"AAA": "trailing", "BBB": "trailing", "CCC": "watched_stop"},
    )

    assert [(e.category, e.kind, e.title) for e in events] == [
        ("held_risk", "trailing_stop", "ISA: AAA hit its trailing stop"),
        # The stop/target predicate wins over the alerter's signal.
        ("held_risk", "stop_hit", "ISA: BBB at or below its stop"),
        ("held_risk", "watched_stop", "ISA: CCC broke its watched stop"),
    ]
    assert events[0].source_event_id == ("held:unknown-run:all-portfolios:AAA:trailing")
    # No wall clock: an unknown evidence date is said, never invented.
    assert all(e.observed_at is None and e.as_of is None for e in events)
    assert events[0].summary.endswith("The price evidence date is unknown.")


def _note(nid: int, **fields: object) -> Notification:
    return Notification.model_validate(
        {
            "id": nid,
            "category": NotificationCategory.PORTFOLIO,
            "event_type": "import_failed",
            "severity": NotificationSeverity.WARNING,
            "title": f"Note {nid}",
            "created_at": AT - timedelta(days=1),
            **fields,
        }
    )


def test_recent_warning_notifications_become_evidence_events() -> None:
    (event,) = notification_events(
        [_note(7, portfolio_id=1, body="The CSV had no rows.")],
        portfolio_id=1,
        now=AT,
    )

    assert (event.category, event.severity) == ("evidence", "medium")
    assert event.source_event_id == "notification:7"
    assert event.raised_by == "Portfolio"
    assert event.portfolio_id == 1
    assert event.summary == "The CSV had no rows."
    assert event.evidence == [
        EvidenceRefV1(
            kind="notification",
            id="7",
            as_of=(AT - timedelta(days=1)).date(),
            source="portfolio",
        )
    ]


def test_only_queueable_notifications_become_events() -> None:
    notes = [
        _note(1),
        _note(2, severity=NotificationSeverity.ERROR, portfolio_id=1),
        _note(3, severity=NotificationSeverity.INFO),
        _note(4, created_at=AT - timedelta(days=8)),
        _note(5, dismissed_at=AT),
        _note(6, category=NotificationCategory.SOURCE),
        _note(7, category=NotificationCategory.ALERT, ticker="AAA"),
        _note(8, portfolio_id=2),
        _note(9, category=NotificationCategory.ALERT, event_type="x"),
    ]

    events = notification_events(notes, portfolio_id=1, now=AT)

    assert [e.source_event_id for e in events] == [
        "notification:1",
        "notification:2",
        "notification:9",
    ]
    assert events[-1].raised_by == "System"


def test_a_run_wide_notification_item_is_not_urgent() -> None:
    queue = build_attention_queue(
        notification_events([_note(1)], portfolio_id=1, now=AT),
        portfolio_id=1,
        analysis_run_id="run-1",
        unavailable=(),
    )

    assert len(queue.items) == 1
    assert queue.urgent_count == 0
