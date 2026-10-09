from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.strategy_experiment import StrategyExperimentAgent
from app.agents.strategy_experiment.agent import (
    FOUNDRY_BASE_URL,
    StrategyExperimentProposalResult,
)

_PROPOSAL_JSON = (
    '{"parameter_name":"lookback","proposed_value":30,'
    '"effect_summary":"Test a longer window.",'
    '"metric":"total_return","expected_direction":"higher"}'
)


class _FakeLocalClient:
    model_id = "phi-4-mini-local"

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[dict[str, object]] = []
        self.closed = False
        self.models = SimpleNamespace(
            list=lambda: SimpleNamespace(data=[SimpleNamespace(id=self.model_id)])
        )
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_completion)
        )

    def _create_completion(self, **request):
        self.requests.append(request)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self.content, refusal=None),
                )
            ]
        )

    def close(self) -> None:
        self.closed = True


class _FakeAnthropicClient:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.messages = SimpleNamespace(create=self._create_message)

    def _create_message(self, **request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _claude_response(
    text: str = _PROPOSAL_JSON, *, stop_reason: str = "end_turn"
) -> object:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
    )


def _call(agent: StrategyExperimentAgent) -> StrategyExperimentProposalResult | None:
    return agent.propose(
        "Longer windows may help",
        strategy_id="momentum_v1",
        parameters={"lookback": {"type": "integer", "minimum": 2}},
        current_values={"lookback": 20},
    )


def test_claude_is_default_and_gets_only_hypothesis_and_parameter_context() -> None:
    claude = _FakeAnthropicClient(_claude_response())
    local = _FakeLocalClient(_PROPOSAL_JSON)
    agent = StrategyExperimentAgent(
        api_key="test-key", anthropic_client=claude, foundry_client=local
    )

    result = _call(agent)

    assert result is not None
    assert result.provider == "anthropic"
    assert result.model_id == "claude-sonnet-5"
    assert [
        (attempt.model_provider, attempt.model_id, attempt.outcome)
        for attempt in result.attempts
    ] == [("anthropic", "claude-sonnet-5", "selected")]
    assert result.proposal.parameter_name == "lookback"
    assert not local.requests
    request = claude.requests[0]
    assert request["model"] == "claude-sonnet-5"
    assert request["max_tokens"] == 1024
    assert "json_schema" == request["output_config"]["format"]["type"]
    user_content = request["messages"][0]["content"]
    assert "Longer windows may help" in user_content
    assert "strategy_id" in user_content
    assert "declared_parameters" in user_content
    assert "current_values" in user_content
    assert "baseline_run_id" not in user_content
    assert "manifest" not in user_content
    assert "result" not in user_content


@pytest.mark.parametrize(
    "claude_response",
    [
        _claude_response(stop_reason="refusal"),
        _claude_response(stop_reason="max_tokens"),
        _claude_response("not-json"),
        _claude_response(
            '{"parameter_name":"lookback","proposed_value":NaN,'
            '"effect_summary":"Test a longer window.",'
            '"metric":"total_return","expected_direction":"higher"}'
        ),
        RuntimeError("Claude unavailable"),
    ],
)
def test_invalid_or_unavailable_claude_falls_back_to_foundry(claude_response) -> None:
    claude = _FakeAnthropicClient(claude_response)
    local = _FakeLocalClient(_PROPOSAL_JSON)
    result = _call(
        StrategyExperimentAgent(
            api_key="test-key", anthropic_client=claude, foundry_client=local
        )
    )

    assert result is not None
    assert result.provider == "foundry_local"
    assert result.model_id == "phi-4-mini-local"
    assert result.proposal.parameter_name == "lookback"
    assert [
        (attempt.model_provider, attempt.model_id, attempt.outcome)
        for attempt in result.attempts
    ] == [
        ("anthropic", "claude-sonnet-5", "no_valid_proposal"),
        ("foundry_local", "phi-4-mini-local", "selected"),
    ]
    assert len(local.requests) == 1
    assert local.closed is False


def test_missing_anthropic_key_uses_foundry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    local = _FakeLocalClient(_PROPOSAL_JSON)

    result = _call(StrategyExperimentAgent(foundry_client=local))

    assert result is not None
    assert result.provider == "foundry_local"
    assert result.model_id == local.model_id
    assert [
        (attempt.model_provider, attempt.model_id, attempt.outcome)
        for attempt in result.attempts
    ] == [("foundry_local", local.model_id, "selected")]


def test_agent_picks_up_anthropic_key_configured_after_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = StrategyExperimentAgent()
    assert not agent.enabled

    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured-after-start")

    assert agent.enabled
    assert agent.api_key == "configured-after-start"


def test_each_call_returns_its_own_provider_identity() -> None:
    claude = _FakeAnthropicClient(_claude_response(), RuntimeError("retry locally"))
    local = _FakeLocalClient(_PROPOSAL_JSON)
    agent = StrategyExperimentAgent(
        api_key="test-key", anthropic_client=claude, foundry_client=local
    )

    first = _call(agent)
    second = _call(agent)

    assert first is not None and first.provider == "anthropic"
    assert second is not None and second.provider == "foundry_local"
    assert first.model_id == "claude-sonnet-5"
    assert second.model_id == "phi-4-mini-local"


def test_agent_uses_fixed_local_endpoint_without_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeLocalClient(_PROPOSAL_JSON)
    constructors: list[dict[str, object]] = []
    http_clients = []

    def fake_openai(**kwargs):
        http_clients.append(kwargs.pop("http_client"))
        constructors.append(kwargs)
        return client

    monkeypatch.setattr(
        "app.agents.strategy_experiment.agent.openai.OpenAI", fake_openai
    )

    result = _call(StrategyExperimentAgent())

    assert result is not None
    assert result.provider == "foundry_local"
    assert result.model_id == client.model_id
    assert constructors == [{"base_url": FOUNDRY_BASE_URL, "api_key": "foundry-local"}]
    assert len(http_clients) == 1
    assert http_clients[0]._trust_env is False
    assert client.closed
    http_clients[0].close()
    assert FOUNDRY_BASE_URL == "http://localhost:5272/v1"


@pytest.mark.parametrize(
    "content",
    [
        '{"parameter_name":"lookback","proposed_value":30,'
        '"effect_summary":"Test a longer window.",'
        '"metric":"total_return","expected_direction":"higher",'
        '"baseline_run_id":"other-run","enqueue":true}',
        '{"parameter_name":"lookback","proposed_value":NaN,'
        '"effect_summary":"Test a longer window.",'
        '"metric":"total_return","expected_direction":"higher"}',
    ],
)
def test_foundry_rejects_extra_fields_and_non_finite_values(content: str) -> None:
    result = _call(StrategyExperimentAgent(foundry_client=_FakeLocalClient(content)))

    assert result is None
