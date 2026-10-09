from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_backtest_repository,
    get_strategy_experiment_service,
)

client = TestClient(app)


def _detail():
    draft = SimpleNamespace(
        strategy_id="momentum_v1",
        parameter_name="lookback",
        baseline_run_id="baseline-1",
        hypothesis="A longer lookback may improve returns.",
        effect_summary="Test a slightly longer signal window.",
        baseline_value=20,
        proposed_value=21,
        metric=SimpleNamespace(value="total_return"),
        expected_direction=SimpleNamespace(value="higher"),
        model_provider="foundry_local",
        model_id="phi-4-mini-local",
        model_attempts=[
            SimpleNamespace(
                model_provider="anthropic",
                model_id="claude-sonnet-5",
                outcome="no_valid_proposal",
            ),
            SimpleNamespace(
                model_provider="foundry_local",
                model_id="phi-4-mini-local",
                outcome="selected",
            ),
        ],
    )
    experiment = SimpleNamespace(
        id="experiment-1",
        status=SimpleNamespace(value="draft"),
        draft=draft,
        draft_digest="a" * 64,
        conclusion=None,
        comparison=None,
        candidate_run_id=None,
    )
    return SimpleNamespace(
        experiment=experiment,
        locked_manifest_json='{"strategy_id":"momentum_v1","lookback":20}',
        candidate_status=None,
        audit_events=(),
    )


class _BacktestRepo:
    def strategy_experiment_baselines(self):
        return ()


class _ExperimentService:
    def __init__(self) -> None:
        self.approvals: list[tuple[str, str, str]] = []
        self.discards: list[tuple[str, str]] = []

    def list(self):
        return ()

    def attempt_audit(self):
        return ()

    def detail(self, _experiment_id: str):
        return _detail()

    def approve(self, experiment_id: str, digest: str, *, actor: str):
        self.approvals.append((experiment_id, digest, actor))
        return _detail().experiment, SimpleNamespace(id="candidate-1")

    def discard(self, experiment_id: str, digest: str):
        self.discards.append((experiment_id, digest))
        return _detail().experiment


@pytest.fixture
def experiment_services(monkeypatch: pytest.MonkeyPatch):
    repo = _BacktestRepo()
    experiments = _ExperimentService()
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    app.dependency_overrides[get_strategy_experiment_service] = lambda: experiments
    monkeypatch.setenv("APP_AUTH_TOKEN", "gh15-test-token")
    try:
        yield experiments
    finally:
        app.dependency_overrides.pop(get_backtest_repository, None)
        app.dependency_overrides.pop(get_strategy_experiment_service, None)


def test_experiment_list_is_a_separate_route(experiment_services) -> None:
    experiment_list = client.get("/strategy-manager/experiments")

    assert experiment_list.status_code == 200
    assert "Strategy experiments" in experiment_list.text
    assert "Create draft" in experiment_list.text
    assert (
        "Only the hypothesis, Strategy ID, declared parameter definitions"
        in experiment_list.text
    )
    assert (
        "baseline ID and manifest, Strategy source, other run inputs"
        in experiment_list.text
    )
    assert "cannot return a schema-valid proposal" in experiment_list.text
    assert (
        "if it does not propose one valid declared parameter change, no draft is created"
        in experiment_list.text
    )


def test_experiment_detail_shows_proposal_model(experiment_services) -> None:
    response = client.get("/strategy-manager/experiments/experiment-1")

    assert response.status_code == 200
    assert "Proposal model" in response.text
    assert "Foundry Local · phi-4-mini-local" in response.text
    assert "Provider attempts" in response.text
    assert "Claude · claude-sonnet-5 — no valid proposal" in response.text
    assert "Foundry Local · phi-4-mini-local — proposal selected" in response.text


def test_approval_route_requires_explicit_checkbox_and_enqueues_on_approval(
    experiment_services: _ExperimentService,
) -> None:
    form = {"draft_digest": "a" * 64}
    rejected = client.post(
        "/strategy-manager/experiments/experiment-1/approve",
        data=form,
        headers={"X-Auth-Token": "gh15-test-token"},
        follow_redirects=False,
    )

    assert rejected.status_code == 422
    assert experiment_services.approvals == []
    assert "Check the explicit approval box" in rejected.text

    accepted = client.post(
        "/strategy-manager/experiments/experiment-1/approve",
        data={**form, "confirm_approval": "yes"},
        headers={"X-Auth-Token": "gh15-test-token"},
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert accepted.headers["location"].endswith("/experiment-1")
    assert experiment_services.approvals == [("experiment-1", "a" * 64, "api_token")]


def test_discard_route_is_digest_bound(experiment_services: _ExperimentService) -> None:
    response = client.post(
        "/strategy-manager/experiments/experiment-1/discard",
        data={"draft_digest": "a" * 64},
        headers={"X-Auth-Token": "gh15-test-token"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert experiment_services.discards == [("experiment-1", "a" * 64)]
