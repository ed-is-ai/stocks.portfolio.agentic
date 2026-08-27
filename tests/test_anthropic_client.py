"""Unit tests for AnthropicNarrativeClient (#109 Phase 3).

The Anthropic SDK is always mocked — no real API call is ever made. Covers:
disabled (no key) returns None; a successful call parses into
LlmNarrativeDraft; a raised SDK exception, a non-"end_turn" stop_reason
(e.g. a safety refusal), and malformed JSON all degrade to None rather than
raising, matching AnalystAgent.get_llm_client()'s graceful-skip pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from app.integrations.anthropic_client import AnthropicNarrativeClient
from app.schemas.llm_narrative import LlmNarrativeDraft
from app.schemas.market_breadth import MarketBreadth
from app.schemas.market_cycle import MarketCycleContext
from app.schemas.news_context import NewsContext
from app.schemas.sector_allocation import (
    CongressionalBuy,
    SectorAllocationSnapshot,
    SectorShare,
)


def _snapshot() -> SectorAllocationSnapshot:
    return SectorAllocationSnapshot(
        as_of="2026-07-27",
        total_candidates=4,
        shares=[
            SectorShare(sector="Technology", count=3, count_share=0.75, score_share=0.8)
        ],
    )


def _cycle() -> MarketCycleContext:
    return MarketCycleContext(
        as_of=datetime(2026, 7, 27).date(),
        last_decision_date=None,
        days_since_last_decision=None,
        next_meeting_start=None,
        days_to_next_meeting=None,
        phase="mid_cycle",
    )


def _news_context() -> NewsContext:
    return NewsContext(
        items=[],
        sector_queries=["Technology"],
        macro_query="stock market",
        fetched_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        degraded=False,
    )


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._response


class _FakeAnthropicClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, messages: _FakeMessages) -> None:
    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **_kw: _FakeAnthropicClient(messages)
    )


class TestEnabled:
    def test_disabled_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # api_key=None falls back to the env var (mirrors AlphaVantageClient),
        # so isolate from a real ANTHROPIC_API_KEY that may be set in .env.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client = AnthropicNarrativeClient(api_key=None)
        assert client.enabled is False

    def test_enabled_with_api_key(self) -> None:
        client = AnthropicNarrativeClient(api_key="sk-test")
        assert client.enabled is True

    def test_reads_env_var_when_no_explicit_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        client = AnthropicNarrativeClient()
        assert client.enabled is True

    def test_env_var_unset_and_no_explicit_key_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client = AnthropicNarrativeClient()
        assert client.enabled is False


class TestGenerateMarketNarrative:
    def test_disabled_client_returns_none_without_calling_sdk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def _boom(**_kw):
            nonlocal called
            called = True
            raise AssertionError("should not construct a client when disabled")

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(anthropic, "Anthropic", _boom)
        client = AnthropicNarrativeClient(api_key=None)
        result = client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context()
        )
        assert result is None
        assert called is False

    def test_successful_call_parses_draft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = (
            '{"headline": "Tech sector leads scan", '
            '"bullets": [{"text": "Technology remains dominant.", '
            '"cited_domains": []}]}'
        )
        response = SimpleNamespace(
            stop_reason="end_turn", content=[_FakeTextBlock(payload)]
        )
        _patch_anthropic(monkeypatch, _FakeMessages(response=response))

        client = AnthropicNarrativeClient(api_key="sk-test")
        result = client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context()
        )

        assert isinstance(result, LlmNarrativeDraft)
        assert result.headline == "Tech sector leads scan"
        assert len(result.bullets) == 1
        assert result.bullets[0].text == "Technology remains dominant."

    def test_uses_configured_model_and_json_schema_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = '{"headline": "H", "bullets": []}'
        response = SimpleNamespace(
            stop_reason="end_turn", content=[_FakeTextBlock(payload)]
        )
        messages = _FakeMessages(response=response)
        _patch_anthropic(monkeypatch, messages)

        client = AnthropicNarrativeClient(api_key="sk-test")
        client.generate_market_narrative(_snapshot(), [], _cycle(), _news_context())

        assert messages.last_kwargs is not None
        assert messages.last_kwargs["model"] == "claude-sonnet-5"
        assert messages.last_kwargs["output_config"]["format"]["type"] == "json_schema"
        assert "temperature" not in messages.last_kwargs
        assert "top_p" not in messages.last_kwargs

    def test_prompt_includes_breadth_and_congress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[_FakeTextBlock('{"headline": "H", "bullets": []}')],
        )
        messages = _FakeMessages(response=response)
        _patch_anthropic(monkeypatch, messages)

        breadth = MarketBreadth(
            pct_above_200dma=42.0, trend_rising=False, bearish_signal=True, as_of="x"
        )
        congress = [
            CongressionalBuy(
                ticker="AAA", sector="Energy", congress_net=8, senate_net=1
            )
        ]
        client = AnthropicNarrativeClient(api_key="sk-test")
        client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context(), breadth, congress
        )

        assert messages.last_kwargs is not None
        prompt = messages.last_kwargs["messages"][0]["content"]
        assert "S&P 500 market breadth: 42%" in prompt
        assert "fetched today" in prompt
        assert "most heavily net-bought by congress/senate" in prompt.lower()
        assert "AAA | Energy | +8" in prompt

    def test_sdk_exception_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_anthropic(monkeypatch, _FakeMessages(error=RuntimeError("network down")))
        client = AnthropicNarrativeClient(api_key="sk-test")
        result = client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context()
        )
        assert result is None

    def test_refusal_stop_reason_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = SimpleNamespace(stop_reason="refusal", content=[])
        _patch_anthropic(monkeypatch, _FakeMessages(response=response))
        client = AnthropicNarrativeClient(api_key="sk-test")
        result = client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context()
        )
        assert result is None

    def test_max_tokens_stop_reason_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = SimpleNamespace(
            stop_reason="max_tokens", content=[_FakeTextBlock('{"headline": "H"')]
        )
        _patch_anthropic(monkeypatch, _FakeMessages(response=response))
        client = AnthropicNarrativeClient(api_key="sk-test")
        result = client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context()
        )
        assert result is None

    def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = SimpleNamespace(
            stop_reason="end_turn", content=[_FakeTextBlock("not json")]
        )
        _patch_anthropic(monkeypatch, _FakeMessages(response=response))
        client = AnthropicNarrativeClient(api_key="sk-test")
        result = client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context()
        )
        assert result is None

    def test_missing_required_field_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[_FakeTextBlock('{"bullets": []}')],  # missing "headline"
        )
        _patch_anthropic(monkeypatch, _FakeMessages(response=response))
        client = AnthropicNarrativeClient(api_key="sk-test")
        result = client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context()
        )
        assert result is None

    def test_no_text_block_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = SimpleNamespace(stop_reason="end_turn", content=[])
        _patch_anthropic(monkeypatch, _FakeMessages(response=response))
        client = AnthropicNarrativeClient(api_key="sk-test")
        result = client.generate_market_narrative(
            _snapshot(), [], _cycle(), _news_context()
        )
        assert result is None
