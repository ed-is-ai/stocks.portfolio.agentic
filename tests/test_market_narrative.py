"""Tests for app.agents.scanner.market_narrative (#109 Phase 1)."""

from __future__ import annotations

import math
from datetime import date

from app.agents.scanner.market_cycle import get_market_cycle_context
from app.agents.scanner.market_narrative import (
    build_deterministic_narrative,
    load_market_narrative,
    narrative_input_digest,
    save_market_narrative,
)
from app.schemas.market_breadth import MarketBreadth
from app.schemas.market_cycle import MarketCycleContext
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

        assert any(
            "fetched from source" in bullet for bullet in fetched_narrative.bullets
        )
        assert any(
            "retrieved from cache" in bullet for bullet in cached_narrative.bullets
        )

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


# ---------------------------------------------------------------------------
# narrative_input_digest (#377)
# ---------------------------------------------------------------------------


def _cycle(phase: str = "mid_cycle", day: int = 1) -> MarketCycleContext:
    return MarketCycleContext(
        as_of=date(2026, 8, day),
        last_decision_date=date(2026, 7, 30),
        days_since_last_decision=day,
        next_meeting_start=date(2026, 9, 17),
        days_to_next_meeting=40 - day,
        phase=phase,
    )


def _digest_inputs():
    snapshot = SectorAllocationSnapshot(
        as_of="2026-08-28",
        total_candidates=4,
        lookback_days=7,
        shares=[
            SectorShare(
                sector="Technology",
                count=3,
                count_share=0.75,
                score_share=0.8,
                strong_count=2,
                strong_share=0.5,
                myb_count=1,
            ),
            SectorShare(
                sector="Healthcare", count=1, count_share=0.25, score_share=0.2
            ),
        ],
        deltas=[
            SectorDelta(
                sector="Technology",
                prior_share=0.5,
                current_share=0.75,
                delta=0.25,
            ),
        ],
    )
    weights = [
        PortfolioSectorWeight(sector="Technology", count=2, value_share=0.6),
        PortfolioSectorWeight(sector="Energy", count=1, value_share=0.4),
    ]
    breadth = MarketBreadth(
        pct_above_200dma=42.4, smoothed_8ma=40.6, trend_rising=True, as_of="2026-08-28"
    )
    congress = [
        CongressionalBuy(ticker="AAA", sector="Tech", congress_net=8, senate_net=1),
        CongressionalBuy(ticker="BBB", sector="Energy", congress_net=3, senate_net=0),
    ]
    return snapshot, weights, _cycle(), breadth, congress


class TestNarrativeInputDigest:
    def test_stable_across_repeated_calls(self) -> None:
        s, w, c, b, g = _digest_inputs()
        assert narrative_input_digest(s, w, c, b, g) == narrative_input_digest(
            s, w, c, b, g
        )

    def test_changes_when_shares_reordered(self) -> None:
        s, w, c, b, g = _digest_inputs()
        reordered = s.model_copy(update={"shares": list(reversed(s.shares))})
        assert narrative_input_digest(s, w, c, b, g) != narrative_input_digest(
            reordered, w, c, b, g
        )

    def test_changes_when_weights_reordered(self) -> None:
        s, w, c, b, g = _digest_inputs()
        assert narrative_input_digest(s, w, c, b, g) != narrative_input_digest(
            s, list(reversed(w)), c, b, g
        )

    def test_changes_when_congress_reordered(self) -> None:
        s, w, c, b, g = _digest_inputs()
        assert narrative_input_digest(s, w, c, b, g) != narrative_input_digest(
            s, w, c, b, list(reversed(g))
        )

    def test_unchanged_within_quantisation_step(self) -> None:
        s, w, c, b, g = _digest_inputs()
        nudged_w = [w[0].model_copy(update={"value_share": 0.601}), w[1]]
        nudged_b = b.model_copy(update={"pct_above_200dma": 42.1})
        assert narrative_input_digest(s, w, c, b, g) == narrative_input_digest(
            s, nudged_w, c, nudged_b, g
        )

    def test_changes_on_material_value_move(self) -> None:
        s, w, c, b, g = _digest_inputs()
        moved = [w[0].model_copy(update={"value_share": 0.9}), w[1]]
        assert narrative_input_digest(s, w, c, b, g) != narrative_input_digest(
            s, moved, c, b, g
        )

    def test_unaffected_by_clock_fields(self) -> None:
        s, w, c, b, g = _digest_inputs()
        s2 = s.model_copy(update={"as_of": "1999-01-01"})
        c2 = _cycle(day=25)
        b2 = b.model_copy(update={"is_fresh": False, "days_old": 99})
        assert narrative_input_digest(s, w, c, b, g) == narrative_input_digest(
            s2, w, c2, b2, g
        )

    def test_changes_when_retrieval_source_flips(self) -> None:
        s, w, c, b, g = _digest_inputs()
        cached = b.model_copy(update={"retrieval_source": "cached"})
        assert narrative_input_digest(s, w, c, b, g) != narrative_input_digest(
            s, w, c, cached, g
        )

    def test_negative_zero_delta_equals_zero(self) -> None:
        s, w, c, b, g = _digest_inputs()
        d_pos = s.model_copy(
            update={
                "deltas": [
                    SectorDelta(
                        sector="X", prior_share=0.0, current_share=0.0, delta=0.0
                    )
                ]
            }
        )
        d_neg = s.model_copy(
            update={
                "deltas": [
                    SectorDelta(
                        sector="X", prior_share=0.0, current_share=0.0, delta=-0.0
                    )
                ]
            }
        )
        assert narrative_input_digest(d_pos, w, c, b, g) == narrative_input_digest(
            d_neg, w, c, b, g
        )

    def test_zero_delta_sector_excluded(self) -> None:
        s, w, c, b, g = _digest_inputs()
        no_zero = s  # single +0.25 delta
        with_zero = s.model_copy(
            update={
                "deltas": s.deltas
                + [
                    SectorDelta(
                        sector="Zero",
                        prior_share=0.1,
                        current_share=0.1001,
                        delta=0.0001,
                    )
                ]
            }
        )
        assert narrative_input_digest(no_zero, w, c, b, g) == narrative_input_digest(
            with_zero, w, c, b, g
        )

    def test_nan_and_inf_return_none(self) -> None:
        s, w, c, b, g = _digest_inputs()
        nan_w = [w[0].model_copy(update={"value_share": math.nan}), w[1]]
        inf_b = b.model_copy(update={"pct_above_200dma": math.inf})
        assert narrative_input_digest(s, nan_w, c, b, g) is None
        assert narrative_input_digest(s, w, c, inf_b, g) is None

    def test_non_numeric_field_returns_none_not_raises(self) -> None:
        s, w, c, b, g = _digest_inputs()
        bad = b.model_copy(update={"pct_above_200dma": "n/a"})
        bad.__dict__["pct_above_200dma"] = "n/a"  # bypass pydantic coercion
        assert narrative_input_digest(s, w, c, bad, g) is None

    def test_none_breadth_distinct_from_empty_breadth(self) -> None:
        s, w, c, b, g = _digest_inputs()
        zeroed = MarketBreadth(
            pct_above_200dma=0.0,
            smoothed_8ma=0.0,
            trend_rising=None,
            as_of="2026-08-28",
        )
        assert narrative_input_digest(s, w, c, None, g) != narrative_input_digest(
            s, w, c, zeroed, g
        )
