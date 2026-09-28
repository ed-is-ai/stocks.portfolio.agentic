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
from app.schemas.portfolio_risk import RiskFindingV1, RiskPolicyV1, RiskReportV1
from app.schemas.trade import Position
from app.services.portfolio_agent_view import (
    AgentCell,
    PortfolioAgentView,
    RecommendationOutcome,
    agent_slug,
    build_agent_view,
)


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
    outcome: RecommendationOutcome, risk: RiskReportV1 | None
) -> PortfolioAgentView:
    return build_agent_view(1, POSITIONS, outcome, risk)


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
    # 2 high findings + 1 Sell + 1 exit evidence gap.
    assert len(view.attention) == 4
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
    assert view.attention == ("AAA is at or below its stop",)
    assert view.unavailable == ("No Strategy assigned",)


def test_without_holdings_no_strategy_is_not_a_missing_source() -> None:
    view = build_agent_view(None, (), NO_ASSIGNMENT, _risk())

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

    assert view.attention == ()
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
    assert "BBB: exit evidence gap (Missing evidence)" in view.attention


def test_slug_is_dom_safe_and_collision_free() -> None:
    slugs = {agent_slug(t) for t in ("BRK.B", "BRK-B", "BRK_2e_B", "^FTSE")}

    assert len(slugs) == 4
    assert all(s.replace("_", "").isalnum() for s in slugs)
    assert agent_slug("AAA") == "AAA"
