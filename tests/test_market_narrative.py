"""Tests for app.agents.scanner.market_narrative (#109 Phase 1)."""

from __future__ import annotations

from datetime import date

from app.agents.scanner.market_cycle import get_market_cycle_context
from app.agents.scanner.market_narrative import (
    build_deterministic_narrative,
    load_market_narrative,
    save_market_narrative,
)
from app.schemas.market_breadth import MarketBreadth
from app.schemas.market_narrative import MarketNarrative
from app.schemas.sector_allocation import (
    CongressionalBuy,
    PortfolioSectorWeight,
    SectorAllocationSnapshot,
    SectorDelta,
    SectorShare,
)


def _snapshot(deltas: list[SectorDelta] | None = None) -> SectorAllocationSnapshot:
    return SectorAllocationSnapshot(
        as_of="2026-07-27",
        total_candidates=4,
        shares=[
            SectorShare(
                sector="Technology", count=3, count_share=0.75, score_share=0.8
            ),
            SectorShare(
                sector="Healthcare", count=1, count_share=0.25, score_share=0.2
            ),
        ],
        deltas=deltas or [],
    )


class TestBuildDeterministicNarrative:
    def test_headline_uses_top_sector(self) -> None:
        cycle = get_market_cycle_context(date(2026, 2, 15))
        narrative = build_deterministic_narrative(_snapshot(), [], cycle)
        assert "Technology" in narrative.headline

    def test_no_prior_run_note_when_deltas_empty(self) -> None:
        cycle = get_market_cycle_context(date(2026, 2, 15))
        narrative = build_deterministic_narrative(_snapshot(), [], cycle)
        assert any("first sector-prevalence snapshot" in b for b in narrative.bullets)

    def test_gainers_and_losers_bulleted(self) -> None:
        deltas = [
            SectorDelta(
                sector="Technology",
                prior_share=0.5,
                current_share=0.75,
                delta=0.25,
            ),
            SectorDelta(
                sector="Healthcare",
                prior_share=0.5,
                current_share=0.25,
                delta=-0.25,
            ),
        ]
        cycle = get_market_cycle_context(date(2026, 2, 15))
        narrative = build_deterministic_narrative(_snapshot(deltas), [], cycle)
        joined = " ".join(narrative.bullets)
        assert "Gaining prevalence" in joined
        assert "Losing prevalence" in joined

    def test_portfolio_bullet_included_when_weights_given(self) -> None:
        weights = [PortfolioSectorWeight(sector="Technology", count=2, value_share=0.6)]
        cycle = get_market_cycle_context(date(2026, 2, 15))
        narrative = build_deterministic_narrative(_snapshot(), weights, cycle)
        assert any("Current holdings concentrated in" in b for b in narrative.bullets)

    def test_week_on_week_wording_uses_lookback_days(self) -> None:
        deltas = [
            SectorDelta(
                sector="Technology",
                prior_share=0.5,
                current_share=0.75,
                delta=0.25,
            )
        ]
        snapshot = _snapshot(deltas).model_copy(update={"lookback_days": 7})
        cycle = get_market_cycle_context(date(2026, 2, 15))
        narrative = build_deterministic_narrative(snapshot, [], cycle)
        assert any("over the past ~7d" in b for b in narrative.bullets)

    def test_breadth_and_congress_and_myb_bullets(self) -> None:
        snapshot = _snapshot().model_copy(
            update={
                "shares": [
                    SectorShare(
                        sector="Energy",
                        count=2,
                        count_share=0.5,
                        score_share=0.5,
                        myb_count=2,
                    )
                ]
            }
        )
        breadth = MarketBreadth(
            pct_above_200dma=42.0, trend_rising=False, bearish_signal=True, as_of="x"
        )
        congress = [
            CongressionalBuy(
                ticker="AAA", sector="Energy", congress_net=8, senate_net=1
            )
        ]
        cycle = get_market_cycle_context(date(2026, 2, 15))
        narrative = build_deterministic_narrative(
            snapshot, [], cycle, breadth, congress
        )
        joined = " ".join(narrative.bullets)
        assert "S&P 500 breadth: 42% of members above their 200DMA" in joined
        assert "breadth-divergence flag set" in joined
        assert "Most heavily net-bought by Congress/Senate: AAA (+8)" in joined
        assert "Most multi-year breakouts by sector: Energy (2)" in joined

    def test_breadth_bullet_shows_fetch_or_cache_provenance(self) -> None:
        cycle = get_market_cycle_context(date(2026, 2, 15))
        fetched = MarketBreadth(pct_above_200dma=50, as_of="x")
        cached = fetched.model_copy(update={"retrieval_source": "cached"})

        fetched_narrative = build_deterministic_narrative(
            _snapshot(), [], cycle, fetched
        )
        cached_narrative = build_deterministic_narrative(_snapshot(), [], cycle, cached)

        assert any("fetched from source" in bullet for bullet in fetched_narrative.bullets)
        assert any("retrieved from cache" in bullet for bullet in cached_narrative.bullets)

    def test_not_advice_note_always_present(self) -> None:
        cycle = get_market_cycle_context(date(2026, 2, 15))
        narrative = build_deterministic_narrative(_snapshot(), [], cycle)
        assert "not financial advice" in narrative.not_advice.lower()

    def test_empty_snapshot_uses_fallback_headline(self) -> None:
        empty = SectorAllocationSnapshot(as_of="2026-07-27", total_candidates=0)
        cycle = get_market_cycle_context(date(2026, 2, 15))
        narrative = build_deterministic_narrative(empty, [], cycle)
        assert narrative.headline == "Sector-allocation snapshot"


class TestMarketNarrativePersistence:
    def test_missing_file_returns_none(self, tmp_path, monkeypatch) -> None:
        import app.agents.scanner.market_narrative as mn

        monkeypatch.setattr(mn, "MARKET_NARRATIVE_JSON", tmp_path / "missing.json")
        assert load_market_narrative() is None

    def test_round_trip_save_and_load(self, tmp_path, monkeypatch) -> None:
        import app.agents.scanner.market_narrative as mn

        monkeypatch.setattr(mn, "MARKET_NARRATIVE_JSON", tmp_path / "narrative.json")
        narrative = MarketNarrative(headline="Test headline", bullets=["a", "b"])
        save_market_narrative(narrative)
        loaded = load_market_narrative()
        assert loaded is not None
        assert loaded.headline == "Test headline"
        assert loaded.bullets == ["a", "b"]

    def test_corrupt_file_returns_none(self, tmp_path, monkeypatch) -> None:
        import app.agents.scanner.market_narrative as mn

        f = tmp_path / "narrative.json"
        f.write_text("not json", encoding="utf-8")
        monkeypatch.setattr(mn, "MARKET_NARRATIVE_JSON", f)
        assert load_market_narrative() is None


def _skill_prompt_text() -> str:
    """Extract the fenced prompt body from the market-narrative skill reference."""
    from app.core.config import SKILLS_DIR

    ref = SKILLS_DIR / "market-narrative" / "references" / "system_prompt.md"
    body = ref.read_text(encoding="utf-8")
    marker = "```text\n"
    start = body.index(marker) + len(marker)
    end = body.index("\n```", start)
    return body[start:end]


class TestSystemPromptDriftGuard:
    """The skill reference must mirror the live `_SYSTEM_PROMPT` verbatim.

    The market-narrative skill (skills/market-narrative/) documents the prompt
    as the versioned source of truth; this asserts it never drifts from the
    prompt the pipeline actually sends.
    """

    def test_skill_reference_matches_live_prompt(self) -> None:
        from app.integrations.anthropic_client import _SYSTEM_PROMPT

        assert _skill_prompt_text() == _SYSTEM_PROMPT
