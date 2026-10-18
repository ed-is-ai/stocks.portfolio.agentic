"""GH-20.4: insight handoff eligibility, rendering, and no implicit draft/job."""

from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.api.app import app
from app.api.templating import templates
from app.api.dependencies import (
    get_backtest_repository,
    get_strategy_experiment_service,
)
from app.schemas.strategy_insights import (
    StrategyInsightClaimV1,
    StrategyInsightParameterV1,
    StrategyQuestionContextV1,
)
from app.services.backtest.strategy_insight_handoff import handoff_url

RUNS = (
    SimpleNamespace(evidence_handle="R01", run_id="run-1", strategy_id="alpha"),
    SimpleNamespace(evidence_handle="R02", run_id="run-2", strategy_id="beta"),
)
STRATEGIES = {"alpha": object(), "beta": object()}


def _declared(_row, _strategy):
    return StrategyQuestionContextV1(
        strategy_id="alpha",
        strategy_api_version=1,
        declared_parameters=(
            StrategyInsightParameterV1(name="lookback", value=20),
            StrategyInsightParameterV1(name="min_volume", value=1.5),
        ),
    )


def _url(
    text, handles=("R01",), baselines=frozenset({"run-1", "run-2"}), declared=_declared
):
    claim = StrategyInsightClaimV1(text=text, evidence_handles=handles, strategy_ids=())
    return handoff_url(
        claim,
        cast(Any, RUNS),
        baseline_ids=baselines,
        strategies=cast(Any, STRATEGIES),
        declared=declared,
    )


def test_single_declared_parameter_is_prefilled_for_review() -> None:
    url = _url("A shorter lookback might improve the return.")

    assert url is not None
    query = parse_qs(urlparse(url).query)
    assert query["baseline_run_id"] == ["run-1"]
    assert query["hypothesis"] == ["A shorter lookback might improve the return."]
    assert _url("Lowering min volume may help.") is not None


def test_unsupported_ideas_are_research_only() -> None:
    assert _url("A new breakout concept could work.") is None  # no parameter
    assert _url("Tune lookback and min_volume together.") is None  # multi-parameter
    assert _url("Lookback matters.", handles=("R01", "R02")) is None  # mixed Strategy
    assert _url("Lookback matters.", handles=("R99",)) is None  # unknown evidence
    assert _url("Lookback matters.", baselines=frozenset()) is None  # stale baseline
    assert _url("Lookback matters.", declared=lambda *_: None) is None  # source drift
    assert _url("The lookbacks varied.") is None  # not a whole-word match


def _render(handoffs):
    claim = StrategyInsightClaimV1(
        text="Try lookback 10.", evidence_handles=("R01",), strategy_ids=()
    )
    report = SimpleNamespace(
        observations=(),
        hypotheses=(claim,),
        strategies_to_explore=(
            SimpleNamespace(
                title="New idea", description="d", evidence_handles=("R01",)
            ),
        ),
    )
    row = SimpleNamespace(evidence_handle="R01", result_url="/r/1")
    return templates.get_template("_strategy_insights_panel.html").render(
        outcome_summary=SimpleNamespace(
            runs=(row,), cohort=SimpleNamespace(limitations=())
        ),
        agent_outcome=SimpleNamespace(
            state="completed", report=report, attempts=(), cached=False, message=None
        ),
        experiment_handoffs=handoffs,
    )


def test_panel_offers_explore_only_for_eligible_hypotheses() -> None:
    eligible = _render({0: "/strategy-manager/experiments?x=1"})
    assert 'href="/strategy-manager/experiments?x=1"' in eligible
    assert "Explore as an experiment" in eligible
    assert "<form" in eligible and eligible.count("<form") == 1  # only Generate
    unsupported = _render({})
    assert "Explore as an experiment" not in unsupported
    assert "Research-only" in unsupported


def test_explore_link_prefills_form_without_drafting() -> None:
    class Experiments:
        def list(self):
            return ()

        def attempt_audit(self):
            return ()

        def draft(self, **_):
            raise AssertionError("opening the form must not draft")

    repo = SimpleNamespace(strategy_experiment_baselines=lambda: ())
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    app.dependency_overrides[get_strategy_experiment_service] = lambda: Experiments()
    try:
        response = TestClient(app).get(
            "/strategy-manager/experiments",
            params={"baseline_run_id": "run-1", "hypothesis": "Try lookback 10."},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Try lookback 10." in response.text
