from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.strategy_experiment import StrategyExperimentAgent
from app.agents.strategy_experiment.agent import FOUNDRY_BASE_URL


class _FakeLocalClient:
    model_id = "phi-4-mini-local"

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[dict[str, object]] = []
        self.models = SimpleNamespace(
            list=lambda: SimpleNamespace(data=[SimpleNamespace(id=self.model_id)])
        )
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_completion)
        )

    def _create_completion(self, **request):
        self.requests.append(request)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def test_agent_uses_fixed_local_foundry_endpoint_and_parses_strict_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeLocalClient(
        '{"parameter_name":"lookback","proposed_value":30,'
        '"effect_summary":"Test a longer window.",'
        '"metric":"total_return","expected_direction":"higher"}'
    )
    constructors: list[dict[str, object]] = []
    http_clients = []

    def fake_openai(**kwargs):
        http_clients.append(kwargs.pop("http_client"))
        constructors.append(kwargs)
        return client

    monkeypatch.setattr("app.agents.strategy_experiment.agent.openai.OpenAI", fake_openai)

    proposal = StrategyExperimentAgent().propose(
        "Longer windows may help",
        strategy_id="momentum_v1",
        parameters={"lookback": {"type": "integer", "minimum": 2}},
        current_values={"lookback": 20},
    )

    assert proposal is not None
    assert proposal.parameter_name == "lookback"
    assert constructors == [{"base_url": FOUNDRY_BASE_URL, "api_key": "foundry-local"}]
    assert len(http_clients) == 1
    assert http_clients[0]._trust_env is False
    http_clients[0].close()
    assert FOUNDRY_BASE_URL == "http://localhost:5272/v1"
    request_text = str(client.requests[0])
    assert "Longer windows may help" in request_text
    assert "baseline_run_id" not in request_text


def test_agent_rejects_run_pins_and_enqueue_fields_from_model_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeLocalClient(
        '{"parameter_name":"lookback","proposed_value":30,'
        '"effect_summary":"Test a longer window.",'
        '"metric":"total_return","expected_direction":"higher",'
        '"baseline_run_id":"other-run","enqueue":true}'
    )
    monkeypatch.setattr(
        "app.agents.strategy_experiment.agent.openai.OpenAI", lambda **_kwargs: client
    )

    proposal = StrategyExperimentAgent().propose(
        "Test one change",
        strategy_id="momentum_v1",
        parameters={"lookback": {"type": "integer"}},
        current_values={"lookback": 20},
    )

    assert proposal is None


def test_agent_rejects_non_finite_parameter_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeLocalClient(
        '{"parameter_name":"lookback","proposed_value":NaN,'
        '"effect_summary":"Test a longer window.",'
        '"metric":"total_return","expected_direction":"higher"}'
    )
    monkeypatch.setattr(
        "app.agents.strategy_experiment.agent.openai.OpenAI", lambda **_kwargs: client
    )

    proposal = StrategyExperimentAgent().propose(
        "Test one change",
        strategy_id="momentum_v1",
        parameters={"lookback": {"type": "integer"}},
        current_values={"lookback": 20},
    )

    assert proposal is None
