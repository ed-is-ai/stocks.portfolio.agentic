"""Tests for the hallucination guard (#109 Phase 3 core requirement).

Covers: a clean draft passes through unchanged; a draft naming an outlet or
citing a domain absent from the fed ``NewsContext`` is caught and
dropped/flagged; and (via ``build_llm_narrative``) an empty/degraded context
results in the deterministic fallback being used instead of calling Claude
at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.agents.scanner.market_cycle import get_market_cycle_context
from app.agents.scanner.market_narrative import build_llm_narrative
from app.agents.scanner.narrative_guard import validate_against_context
from app.integrations.anthropic_client import AnthropicNarrativeClient
from app.schemas.llm_narrative import LlmNarrativeBullet, LlmNarrativeDraft
from app.schemas.news_context import NewsContext, NewsItem
from app.schemas.sector_allocation import SectorAllocationSnapshot, SectorShare


def _news_context(
    items: list[NewsItem] | None = None, degraded: bool = False
) -> NewsContext:
    return NewsContext(
        items=items or [],
        sector_queries=["Technology"],
        macro_query="stock market",
        fetched_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        degraded=degraded,
    )


def _fed_item(
    domain: str = "reuters.com", title: str = "Fed signals rate pause"
) -> NewsItem:
    return NewsItem(
        title=title,
        domain=domain,
        url=f"https://{domain}/article",
        sentiment=0.1,
        seen_date=datetime(2026, 7, 27, tzinfo=timezone.utc),
        source_feed="alpha_vantage",
    )


def _snapshot() -> SectorAllocationSnapshot:
    return SectorAllocationSnapshot(
        as_of="2026-07-27",
        total_candidates=4,
        shares=[
            SectorShare(sector="Technology", count=3, count_share=0.75, score_share=0.8)
        ],
    )


class TestValidateAgainstContext:
    def test_clean_draft_passes_through(self) -> None:
        context = _news_context([_fed_item("reuters.com")])
        draft = LlmNarrativeDraft(
            headline="Tech sector leads scan",
            bullets=[
                LlmNarrativeBullet(
                    text="Reuters reports the Fed signalled a rate pause.",
                    cited_domains=["reuters.com"],
                ),
                LlmNarrativeBullet(
                    text="Technology remains the top sector.", cited_domains=[]
                ),
            ],
        )
        result = validate_against_context(draft, context)
        assert result.valid is True
        assert result.dropped_bullets == []
        assert len(result.kept_bullets) == 2
        assert result.cited_domains == ["reuters.com"]

    def test_www_prefixed_domain_matches_fed_bare_domain(self) -> None:
        context = _news_context([_fed_item("reuters.com")])
        draft = LlmNarrativeDraft(
            headline="Tech sector leads scan",
            bullets=[
                LlmNarrativeBullet(
                    text="Rates paused.", cited_domains=["www.reuters.com"]
                )
            ],
        )
        result = validate_against_context(draft, context)
        assert result.valid is True
        assert result.kept_bullets == ["Rates paused."]

    def test_bullet_citing_unfed_domain_is_dropped(self) -> None:
        context = _news_context([_fed_item("reuters.com")])
        draft = LlmNarrativeDraft(
            headline="Tech sector leads scan",
            bullets=[
                LlmNarrativeBullet(
                    text="Bloomberg says rates will fall.",
                    cited_domains=["bloomberg.com"],
                ),
                LlmNarrativeBullet(
                    text="Technology remains the top sector.", cited_domains=[]
                ),
            ],
        )
        result = validate_against_context(draft, context)
        assert result.valid is True
        assert result.kept_bullets == ["Technology remains the top sector."]
        assert result.dropped_bullets == ["Bloomberg says rates will fall."]

    def test_bullet_naming_unfed_outlet_without_citation_is_dropped(self) -> None:
        """Free-text outlet mention is caught even without a cited_domains tag."""
        context = _news_context([_fed_item("example.com")])
        draft = LlmNarrativeDraft(
            headline="Tech sector leads scan",
            bullets=[
                LlmNarrativeBullet(
                    text="According to CNBC, rate cuts are expected.", cited_domains=[]
                )
            ],
        )
        result = validate_against_context(draft, context)
        assert result.valid is True
        assert result.kept_bullets == []
        assert result.dropped_bullets == ["According to CNBC, rate cuts are expected."]

    def test_headline_naming_unfed_outlet_rejects_whole_draft(self) -> None:
        context = _news_context([_fed_item("example.com")])
        draft = LlmNarrativeDraft(
            headline="Reuters: tech sector leads scan",
            bullets=[LlmNarrativeBullet(text="Technology leads.", cited_domains=[])],
        )
        result = validate_against_context(draft, context)
        assert result.valid is False
        assert result.kept_bullets == []

    def test_empty_context_flags_any_outlet_mention(self) -> None:
        context = _news_context([])
        draft = LlmNarrativeDraft(
            headline="Tech sector leads scan",
            bullets=[
                LlmNarrativeBullet(text="Bloomberg reports a rally.", cited_domains=[])
            ],
        )
        result = validate_against_context(draft, context)
        assert result.valid is True
        assert result.kept_bullets == []
        assert result.dropped_bullets == ["Bloomberg reports a rally."]


class TestBuildLlmNarrativeFallback:
    def test_disabled_client_returns_none(self) -> None:
        cycle = get_market_cycle_context()
        client = AnthropicNarrativeClient(api_key=None)
        result = build_llm_narrative(_snapshot(), [], cycle, _news_context(), client)
        assert result is None

    def test_degraded_context_returns_none_without_calling_claude(
        self, monkeypatch
    ) -> None:
        cycle = get_market_cycle_context()
        client = AnthropicNarrativeClient(api_key="test-key")

        def _boom(*_args, **_kwargs):
            raise AssertionError("Claude should not be called for degraded context")

        monkeypatch.setattr(client, "generate_market_narrative", _boom)
        result = build_llm_narrative(
            _snapshot(), [], cycle, _news_context(degraded=True), client
        )
        assert result is None

    def test_empty_items_returns_none_without_calling_claude(self, monkeypatch) -> None:
        cycle = get_market_cycle_context()
        client = AnthropicNarrativeClient(api_key="test-key")

        def _boom(*_args, **_kwargs):
            raise AssertionError("Claude should not be called for empty context")

        monkeypatch.setattr(client, "generate_market_narrative", _boom)
        result = build_llm_narrative(
            _snapshot(), [], cycle, _news_context(items=[]), client
        )
        assert result is None

    def test_claude_returning_none_falls_back(self, monkeypatch) -> None:
        cycle = get_market_cycle_context()
        client = AnthropicNarrativeClient(api_key="test-key")
        monkeypatch.setattr(client, "generate_market_narrative", lambda *a, **k: None)
        context = _news_context([_fed_item()])
        result = build_llm_narrative(_snapshot(), [], cycle, context, client)
        assert result is None

    def test_all_bullets_dropped_falls_back(self, monkeypatch) -> None:
        cycle = get_market_cycle_context()
        client = AnthropicNarrativeClient(api_key="test-key")
        context = _news_context([_fed_item("example.com")])
        draft = LlmNarrativeDraft(
            headline="Tech sector leads scan",
            bullets=[
                LlmNarrativeBullet(text="Bloomberg says rates fall.", cited_domains=[])
            ],
        )
        monkeypatch.setattr(client, "generate_market_narrative", lambda *a, **k: draft)
        result = build_llm_narrative(_snapshot(), [], cycle, context, client)
        assert result is None

    def test_valid_draft_builds_narrative_with_sources(self, monkeypatch) -> None:
        cycle = get_market_cycle_context()
        client = AnthropicNarrativeClient(api_key="test-key")
        context = _news_context([_fed_item("reuters.com")])
        draft = LlmNarrativeDraft(
            headline="Tech sector leads scan",
            bullets=[
                LlmNarrativeBullet(
                    text="Reuters reports a rate pause.", cited_domains=["reuters.com"]
                )
            ],
        )
        monkeypatch.setattr(client, "generate_market_narrative", lambda *a, **k: draft)
        result = build_llm_narrative(_snapshot(), [], cycle, context, client)
        assert result is not None
        assert result.headline == "Tech sector leads scan"
        assert result.bullets == ["Reuters reports a rate pause."]
        assert len(result.sources) == 1
        assert result.sources[0].label == "reuters.com"
        assert result.sources[0].url == "https://reuters.com/article"
        assert "not financial advice" in result.not_advice.lower()
