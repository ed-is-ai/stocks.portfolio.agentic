"""Tests for app.agents.scanner.market_narrative (#109 Phase 1)."""

from __future__ import annotations

from datetime import date

from app.agents.scanner.market_cycle import get_market_cycle_context
from app.agents.scanner.market_narrative import (
    build_deterministic_narrative,
    load_market_narrative,
    save_market_narrative,
)
from app.schemas.market_narrative import MarketNarrative
from app.schemas.sector_allocation import (
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
