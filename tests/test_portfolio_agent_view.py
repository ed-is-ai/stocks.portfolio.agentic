"""Unit tests for the pure Portfolio agent-layer builder (GH-19)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.schemas.portfolio_recommendation import (
    NO_ASSIGNMENT,
    EvaluationCoverageV1,
    EvaluationUnavailable,
    RecommendationEvidenceDiagnosticV1,
    RecommendationResultV1,
    RecommendationV1,
)
from app.agents.thesis.evaluator import evaluate_thesis
from app.agents.triage.sources import freshness_events, source_health_events
from app.schemas.portfolio_risk import RiskFindingV1, RiskPolicyV1, RiskReportV1
from app.schemas.position_thesis import ThesisSummary
from app.schemas.source_health import SourceHealth, SourceName, SourceState
from app.schemas.trade import Position
from app.services.freshness_service import Freshness, FreshnessState
from app.services.portfolio_agent_view import (
    AgentCell,
    PortfolioAgentView,
    PublishedEvidence,
    RecommendationOutcome,
    ThesisStates,
    agent_slug,
    build_agent_view,
)
from tests.test_thesis_evaluator import FRESH, META, make_record, make_thesis


def _position(ticker: str, display: str | None = None) -> Position:
    return Position(
        ticker=ticker, shares=1, avg_cost=1, total_cost=1, display_ticker=display
    )


POSITIONS = [_position("AAA"), _position("0P0.L", "BBB")]


def _rec(action: str, security_id: str, ticker: str) -> RecommendationV1:
    return RecommendationV1.model_validate(
        {
            "action": action,
            "ticker": ticker,
            "security_id": security_id,
            "rule_id": "r",
            "reason": f"{action} reason",
        }
    )


def _diagnostic(
    security_id: str, available: int, path: str = "exit"
) -> RecommendationEvidenceDiagnosticV1:
    return RecommendationEvidenceDiagnosticV1.model_validate(
        {
            "security_id": security_id,
            "display_ticker": security_id,
            "path": path,
            "required_sessions": 200,
            "available_sessions": available,
            "cause": "short_history",
            "disposition": "hold",
        }
    )


def _result(
    *diagnostics: RecommendationEvidenceDiagnosticV1,
    coverage: dict[str, object] | None = None,
) -> RecommendationResultV1:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    return RecommendationResultV1(
        portfolio_id=1,
        analysis_run_id="run-1",
        generated_at=now,
        market_session=date(2026, 9, 25),
        freshness="fresh",
        strategy_id="alpha",
        strategy_source_digest="a" * 64,
        parameters={},
        recommendations=(_rec("sell", "AAA", "AAA"), _rec("hold", "0P0.L", "BBB")),
        coverage=EvaluationCoverageV1.model_validate(
            {"diagnostics": diagnostics, **(coverage or {})}
        ),
        evaluated_at=now,
    )


def _finding(kind: str, severity: str, title: str, *tickers: str) -> RiskFindingV1:
    return RiskFindingV1.model_validate(
        {
            "kind": kind,
            "severity": severity,
            "title": title,
            "detail": "",
            "tickers": tickers,
        }
    )


def _risk(
    *findings: RiskFindingV1,
    at_risk: tuple[str, str] | None = ("150.00", "7.5"),
) -> RiskReportV1:
    return RiskReportV1(
        policy=RiskPolicyV1(),
        findings=findings,
        confidence="complete",
        limitations=(),
        total_value_gbp=Decimal("2000"),
        position_weights={"AAA": Decimal("50.0"), "BBB": Decimal("10.0")},
        capital_at_risk_gbp=None if at_risk is None else Decimal(at_risk[0]),
        capital_at_risk_pct=None if at_risk is None else Decimal(at_risk[1]),
    )


def _view(
    outcome: RecommendationOutcome,
    risk: RiskReportV1 | None,
    theses: ThesisStates = {},  # noqa: B006 — read-only
) -> PortfolioAgentView:
    return build_agent_view(1, POSITIONS, outcome, risk, theses)


def test_all_live_sources_join_onto_holdings() -> None:
    concentration = _finding(
        "position_concentration", "high", "AAA is 50.0% of the portfolio", "AAA"
    )
    capital = _finding(
        "capital_at_risk", "high", "Capital at risk to stops is 7.5%", "AAA", "BBB"
    )
    view = _view(
        _result(_diagnostic("0P0.L", 166), _diagnostic("AAA", 10, path="entry")),
        _risk(concentration, capital),
    )

    aaa, bbb = view.rows
    assert view.portfolio_id == 1
    assert aaa.strategy == AgentCell("Sell", "risk", "sell reason")
    assert bbb.strategy == AgentCell("Hold", "good", "hold reason")
    # Weight by display symbol; the high finding flags AAA's row.
    assert aaa.risk == AgentCell("50.0%", "risk", "AAA is 50.0% of the portfolio")
    assert bbb.risk == AgentCell("10.0%", "muted", "Within policy")
    # Only the exit path counts for a holding; an entry diagnostic is ignored.
    assert aaa.evidence == AgentCell("Complete", "good")
    assert bbb.evidence.text == "166 / 200"
    # 2 high findings + 1 Sell + 1 exit evidence gap, risk first.
    assert view.attention.urgent_count == 4
    assert [i.kind for i in view.attention.items] == [
        "held_risk",
        "held_risk",
        "exit",
        "evidence",
    ]
    assert view.unavailable == ()
    assert view.open_risk == AgentCell("£150.00 · 7.5%", "risk")


def test_portfolio_wide_findings_do_not_flag_every_row() -> None:
    capital = _finding("capital_at_risk", "high", "Capital at risk is 9%", "AAA")
    sector = _finding(
        "sector_concentration", "high", "Energy is 60.0% of the portfolio", "AAA"
    )
    view = _view(NO_ASSIGNMENT, _risk(capital, sector))

    assert view.rows[0].risk == AgentCell(
        "50.0%", "muted", "Energy is 60.0% of the portfolio"
    )


def test_open_risk_tone_follows_the_engine_finding() -> None:
    # 7.5% is over the default 6% limit, but the engine said "info".
    info = _finding("capital_at_risk", "info", "Capital at risk is 7.5%", "AAA")

    assert _view(NO_ASSIGNMENT, _risk(info)).open_risk.tone == "muted"


def test_no_strategy_declares_itself_and_counts_risk_only() -> None:
    high = _finding("below_stop", "high", "AAA is at or below its stop", "AAA")
    view = _view(NO_ASSIGNMENT, _risk(high))

    for row in view.rows:
        assert row.strategy.text == "No Strategy assigned"
        assert row.evidence.text == "No Strategy assigned"
    assert _titles(view) == ["AAA is at or below its stop"]
    assert view.unavailable == ("No Strategy assigned",)


def test_without_holdings_no_strategy_is_not_a_missing_source() -> None:
    view = build_agent_view(None, (), NO_ASSIGNMENT, _risk(), {})

    assert view.unavailable == ()


def test_evaluation_unavailable_carries_its_reason() -> None:
    reason = "No published scan artifact to evaluate against."
    view = _view(EvaluationUnavailable(reason=reason), _risk())

    cell = view.rows[0].strategy
    assert (cell.text, cell.note) == ("Strategy unavailable", reason)
    assert view.rows[0].evidence == cell
    assert view.unavailable == ("Strategy unavailable",)


def test_failed_risk_report_is_declared() -> None:
    view = _view(NO_ASSIGNMENT, None)

    assert {row.risk.text for row in view.rows} == {"Risk unavailable"}
    assert view.open_risk.text == "Unavailable"
    assert "Risk unavailable" in view.unavailable


def test_no_evidenced_stops() -> None:
    assert _view(NO_ASSIGNMENT, _risk(at_risk=None)).open_risk.text == (
        "No evidenced stops"
    )


def test_zero_findings() -> None:
    result = _result().model_copy(
        update={"recommendations": (_rec("hold", "AAA", "AAA"),)}
    )
    info = _finding("cash", "info", "Cash: £1,000.00")
    view = _view(result, _risk(info, at_risk=("10", "0.5")))

    assert view.attention.items == []
    assert view.open_risk.tone == "muted"
    # A holding the Strategy returned no row for is not invented, and its
    # evidence is not "Complete" either.
    assert view.rows[1].strategy.text == "Not evaluated"
    assert view.rows[1].evidence.text == "Not evaluated"


def test_degraded_exit_coverage_is_not_complete() -> None:
    view = _view(
        _result(
            coverage={
                "exit_state": "degraded",
                "exit_missing_evidence": ("volume",),
            }
        ),
        _risk(),
    )

    assert view.rows[0].evidence == AgentCell("Degraded", "warn", "volume")


def test_degraded_security_is_not_complete() -> None:
    view = _view(_result(coverage={"degraded_securities": ("AAA",)}), _risk())

    assert view.rows[0].evidence.text == "Degraded"
    assert view.rows[1].evidence.text == "Complete"


def test_non_session_diagnostic_shows_its_cause_and_is_counted() -> None:
    gap = _diagnostic("0P0.L", 0).model_copy(
        update={"required_sessions": 0, "cause": "missing_evidence"}
    )
    view = _view(_result(gap), _risk())

    assert view.rows[1].evidence == AgentCell(
        "Missing evidence", "warn", "exit evidence"
    )
    assert "BBB: exit evidence gap (Missing evidence)" in _titles(view)


def _titles(view: PortfolioAgentView) -> list[str]:
    return [item.title for item in view.attention.items]


def _event_titles(view: PortfolioAgentView) -> list[str]:
    return [event.title for event in view.attention.events]


def _thesis_view(summary: ThesisSummary | None) -> PortfolioAgentView:
    return _view(_result(), _risk(), {} if summary is None else {"AAA": summary})


def test_thesis_cell_states() -> None:
    active = make_thesis({"kind": "close_below_sma", "period": 50})
    draft = make_thesis(
        {"kind": "stage_2_lost"}, id=2, version=2, active=False, text_source="ai_draft"
    )
    invalidated = evaluate_thesis(active, make_record(price=94.0), META, FRESH)
    confirmed = evaluate_thesis(active, make_record(), META, FRESH)

    assert _thesis_view(None).rows[0].thesis == AgentCell("No thesis")
    assert _thesis_view(ThesisSummary(pending=draft)).rows[0].thesis == AgentCell(
        "Draft to confirm", "info"
    )
    assert _thesis_view(ThesisSummary(active=active)).rows[0].thesis == AgentCell(
        "Awaiting scan", "info"
    )
    assert _thesis_view(
        ThesisSummary(active=active, latest=confirmed, review_due=True, current=True)
    ).rows[0].thesis == AgentCell("Confirmed", "good", "Review due")
    view = _thesis_view(
        ThesisSummary(active=active, pending=draft, latest=invalidated, current=True)
    )
    assert view.rows[0].thesis == AgentCell(
        "Invalidated",
        "risk",
        "Rule 1: Close below the 50-day SMA · Draft to confirm",
    )
    assert "AAA: thesis invalidated" in _event_titles(view)
    # The invalidation joins AAA's Sell as one exit item with both events.
    assert view.attention.urgent_count == _thesis_view(None).attention.urgent_count
    exit_item = next(i for i in view.attention.items if i.kind == "exit")
    assert len(exit_item.source_event_ids) == 2


def test_thesis_store_failure_is_declared() -> None:
    view = _view(_result(), _risk(), None)

    assert {row.thesis.text for row in view.rows} == {"Thesis unavailable"}
    assert "Thesis unavailable" in view.unavailable


def test_slug_is_dom_safe_and_collision_free() -> None:
    slugs = {agent_slug(t) for t in ("BRK.B", "BRK-B", "BRK_2e_B", "^FTSE")}

    assert len(slugs) == 4
    assert all(s.replace("_", "").isalnum() for s in slugs)
    assert agent_slug("AAA") == "AAA"


def test_thesis_result_of_an_earlier_run_is_limited_and_queued_stale() -> None:
    active = make_thesis({"kind": "close_below_sma", "period": 50})
    invalidated = evaluate_thesis(active, make_record(price=94.0), META, FRESH)

    view = _thesis_view(ThesisSummary(active=active, latest=invalidated))

    assert view.rows[0].thesis == AgentCell(
        "Evidence limited", "warn", "last checked on an earlier run"
    )
    stale = next(e for e in view.attention.events if e.kind == "thesis_invalidated")
    assert stale.stale
    assert stale.title == "Stale: AAA: thesis invalidated"
    # Grouped with AAA's fresh Sell, the item is not described as stale.
    exit_item = view.attention.items[-1]
    assert "(includes stale evidence)" in exit_item.summary
    assert not exit_item.title.startswith("Stale:")


def test_failed_sources_queue_the_rest_and_are_listed_unavailable() -> None:
    view = _view(_result(_diagnostic("0P0.L", 166)), None, None)

    assert [item.kind for item in view.attention.items] == ["exit", "evidence"]
    assert view.attention.unavailable == ["Risk unavailable", "Thesis unavailable"]
    assert view.attention.analysis_run_id == "run-1"


def test_published_evidence_joins_the_queue() -> None:
    source = SourceHealth(source=SourceName.CONGRESS, state=SourceState.FAILED)
    stale = Freshness(
        state=FreshnessState.STALE, refreshed_at=datetime(2026, 9, 20, tzinfo=UTC)
    )
    published = PublishedEvidence(
        run_id="run-9",
        events=(
            *source_health_events({SourceName.CONGRESS: source}, run_id="run-9"),
            *freshness_events(stale, run_id="run-9"),
        ),
    )
    view = build_agent_view(1, POSITIONS, NO_ASSIGNMENT, _risk(), {}, published)

    assert [item.title for item in view.attention.items] == [
        "Analysis is stale (as of 2026-09-20)",
        "Congress source failed",
    ]
    assert view.attention.analysis_run_id == "run-9"
    # Run-wide evidence is listed but never counted against this portfolio.
    assert view.attention.urgent_count == 0
