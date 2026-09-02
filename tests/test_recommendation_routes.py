"""Route tests for the recommendations screen (#441).

TestClient + dependency_overrides; the real Jinja template renders to
catch markup errors. Every state renders 200 — never a 500.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_portfolio_recommendation_service,
    get_trader_service,
)
from app.schemas.portfolio_recommendation import (
    EvaluationCoverageV1,
    EvaluationUnavailable,
    EvidenceState,
    NoAssignment,
    RecommendationResultV1,
    RecommendationV1,
)

client = TestClient(app)

SESSION = date(2026, 8, 28)


def _result(freshness: str = "fresh") -> RecommendationResultV1:
    return RecommendationResultV1(
        portfolio_id=7,
        analysis_run_id="run-1",
        generated_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        market_session=SESSION,
        freshness=cast(Any, freshness),
        strategy_id="alpha",
        strategy_source_digest="a" * 64,
        parameters={"lookback": 20},
        recommendations=(
            RecommendationV1(
                action="sell",
                ticker="AAA",
                security_id="AAA",
                rule_id="exit_history_fall",
                reason="Strategy rule exit_history_fall",
            ),
            RecommendationV1(
                action="hold",
                ticker="BBB",
                security_id="BBB",
                rule_id="no_exit_signal",
                reason="No exit signal from the assigned Strategy — position held.",
            ),
            RecommendationV1(
                action="buy",
                ticker="CCC",
                security_id="CCC",
                rule_id="entry_history_rise",
                reason="Strategy rule entry_history_rise",
            ),
        ),
        evaluated_at=datetime.now(UTC),
    )


@pytest.fixture
def mocked(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    stub = MagicMock()
    trader = MagicMock()
    trader.get_portfolio_meta.return_value.name = "SIPP"
    app.dependency_overrides[get_portfolio_recommendation_service] = lambda: stub
    app.dependency_overrides[get_trader_service] = lambda: trader
    try:
        yield {"stub": stub}
    finally:
        app.dependency_overrides.clear()


def test_happy_path_renders_groups_in_order(mocked: dict[str, Any]) -> None:
    mocked["stub"].recommend.return_value = _result()
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    html = resp.text
    assert "Recommendations — SIPP" in html
    assert "digest aaaaaaaa" in html
    sell_at = html.index("Sell (1)")
    hold_at = html.index("Hold (1)")
    buy_at = html.index("Buy (1)")
    assert sell_at < hold_at < buy_at
    assert "exit_history_fall" in html
    assert "no_exit_signal" in html
    assert "entry_history_rise" in html


def test_stale_warning_renders_alongside_results(mocked: dict[str, Any]) -> None:
    mocked["stub"].recommend.return_value = _result(freshness="stale")
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "alert-warning" in resp.text
    assert "Sell (1)" in resp.text


def test_no_assignment_renders_assign_link(mocked: dict[str, Any]) -> None:
    mocked["stub"].recommend.return_value = NoAssignment()
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert 'hx-get="/partials/strategy-assign?portfolio_id=7"' in resp.text
    assert "No Strategy assigned" in resp.text


def test_evaluation_unavailable_renders_alert_not_500(mocked: dict[str, Any]) -> None:
    mocked["stub"].recommend.return_value = EvaluationUnavailable(
        reason="Strategy runtime could not be loaded: boom"
    )
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "alert-danger" in resp.text
    assert "Strategy runtime could not be loaded: boom" in resp.text
    assert "Retry" in resp.text


def test_all_states_return_200(mocked: dict[str, Any]) -> None:
    outcomes: list[Any] = [
        _result(),
        _result(freshness="missing"),
        NoAssignment(),
        EvaluationUnavailable(reason="x"),
    ]
    for outcome in outcomes:
        mocked["stub"].recommend.return_value = outcome
        resp = client.get("/portfolios/7/recommendations")
        assert resp.status_code == 200


def _incomplete_result(
    entry_state: EvidenceState = "incompatible",
    exit_state: EvidenceState = "incompatible",
    missing: tuple[str, ...] = ("scan_stage",),
) -> RecommendationResultV1:
    """A result whose evidence was incomplete — no Sell/Buy rows (#471)."""
    base = _result()
    return base.model_copy(
        update={
            "recommendations": (
                RecommendationV1(
                    action="hold",
                    ticker="AAA",
                    security_id="AAA",
                    rule_id="exit_evidence_unsupported",
                    reason="fails safe to Hold",
                    evidence_warnings=("exit_evidence_unsupported",) + missing,
                ),
            ),
            "coverage": EvaluationCoverageV1(
                entry_state=entry_state,
                exit_state=exit_state,
                entry_missing_evidence=missing,
                exit_missing_evidence=missing,
                evaluated_securities=2,
            ),
        }
    )


def test_incomplete_evidence_replaces_no_signals_wording(
    mocked: dict[str, Any],
) -> None:
    mocked["stub"].recommend.return_value = _incomplete_result()
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    html = resp.text
    assert "Evidence incomplete" in html
    assert "scan_stage" in html
    assert "Signals unavailable" in html
    assert "No Buy signals." not in html
    assert "No Sell signals." not in html
    # Hold was genuinely evaluated, so its own empty wording is unchanged.
    assert "Hold (1)" in html


def test_degraded_evidence_names_the_affected_securities(
    mocked: dict[str, Any],
) -> None:
    result = _incomplete_result(
        entry_state="degraded", exit_state="degraded", missing=()
    ).model_copy(
        update={
            "coverage": EvaluationCoverageV1(
                entry_state="degraded",
                exit_state="degraded",
                evaluated_securities=2,
                degraded_securities=("AAA", "BBB"),
            )
        }
    )
    mocked["stub"].recommend.return_value = result
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "Evidence incomplete" in resp.text
    assert "AAA" in resp.text and "BBB" in resp.text
    # A degraded path IS invoked, so the honest empty wording stays.
    assert "No Buy signals." in resp.text
    assert "Signals unavailable" not in resp.text


def test_complete_evaluation_keeps_the_no_signals_wording(
    mocked: dict[str, Any],
) -> None:
    empty = _result().model_copy(update={"recommendations": ()})
    mocked["stub"].recommend.return_value = empty
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "No Buy signals." in resp.text
    assert "Evidence incomplete" not in resp.text
    assert "Signals unavailable" not in resp.text


def test_one_incompatible_path_scopes_the_wording(mocked: dict[str, Any]) -> None:
    """Only the blocked path's consequence is claimed (#471)."""
    result = _incomplete_result(entry_state="compatible", exit_state="incompatible")
    mocked["stub"].recommend.return_value = result
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    html = resp.text
    assert "holdings fail safe to Hold" in html
    assert "no Buy signals are produced" not in html
    # Sell was never evaluated; Buy was, so only Sell loses its wording.
    assert "Signals unavailable" in html
    assert "No Buy signals." in html
    assert "No Sell signals." not in html


def test_degraded_security_list_is_capped(mocked: dict[str, Any]) -> None:
    """A wide degraded set is summarised rather than dumped (#471)."""
    tickers = tuple(f"T{index:02d}" for index in range(12))
    result = _result().model_copy(
        update={
            "recommendations": (),
            "coverage": EvaluationCoverageV1(
                entry_state="degraded",
                exit_state="degraded",
                evaluated_securities=12,
                degraded_securities=tickers,
            ),
        }
    )
    mocked["stub"].recommend.return_value = result
    resp = client.get("/portfolios/7/recommendations")
    assert resp.status_code == 200
    assert "+2 more" in resp.text
    assert "T09" in resp.text
    assert "T10" not in resp.text
