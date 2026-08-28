"""Orchestrator-level reuse tests for ``resolve_market_narrative`` (#377)."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone

import pytest

import app.agents.scanner.market_narrative as mn
from app.schemas.market_breadth import MarketBreadth
from app.schemas.market_cycle import MarketCycleContext
from app.schemas.market_narrative import MarketNarrative
from app.schemas.news_context import NewsContext, NewsItem
from app.schemas.sector_allocation import (
    CongressionalBuy,
    PortfolioSectorWeight,
    SectorAllocationSnapshot,
    SectorDelta,
    SectorShare,
)


class _AnthropicStub:
    enabled = True


def _inputs():
    snapshot = SectorAllocationSnapshot(
        as_of="2026-08-28",
        total_candidates=2,
        lookback_days=7,
        shares=[
            SectorShare(sector="Technology", count=1, count_share=0.5, score_share=0.5),
            SectorShare(sector="Energy", count=1, count_share=0.5, score_share=0.5),
        ],
        deltas=[
            SectorDelta(
                sector="Technology", prior_share=0.3, current_share=0.5, delta=0.2
            )
        ],
    )
    weights = [PortfolioSectorWeight(sector="Technology", count=1, value_share=1.0)]
    cycle = MarketCycleContext(
        as_of=date(2026, 8, 28),
        last_decision_date=date(2026, 7, 30),
        days_since_last_decision=29,
        next_meeting_start=date(2026, 9, 17),
        days_to_next_meeting=20,
        phase="mid_cycle",
    )
    breadth = MarketBreadth(pct_above_200dma=55.0, as_of="2026-08-28")
    congress = [
        CongressionalBuy(ticker="AAA", sector="Tech", congress_net=5, senate_net=0)
    ]
    return snapshot, weights, cycle, breadth, congress


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point persistence at a temp file and install news/LLM spies."""
    monkeypatch.setattr(mn, "MARKET_NARRATIVE_JSON", tmp_path / "market_narrative.json")

    calls = {"news": 0, "llm": 0}

    def _news_spy(top_sectors, av_client):
        calls["news"] += 1
        return NewsContext(
            items=[
                NewsItem(
                    title="t",
                    domain="example.com",
                    url="https://example.com",
                    source_feed="gdelt",
                )
            ],
            fetched_at=datetime.now(timezone.utc),
            degraded=False,
        )

    def _llm_spy(*args, **kwargs):
        calls["llm"] += 1
        return MarketNarrative(
            headline="LLM headline",
            bullets=["b1"],
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    monkeypatch.setattr(mn, "gather_news_context", _news_spy)
    monkeypatch.setattr(mn, "build_llm_narrative", _llm_spy)
    return calls


def _resolve():
    s, w, c, b, g = _inputs()
    return mn.resolve_market_narrative(s, w, c, b, g, _AnthropicStub(), None)


def test_miss_then_hit(wired) -> None:
    first, reused_first = _resolve()
    assert reused_first is False
    assert wired == {"news": 1, "llm": 1}

    second, reused_second = _resolve()
    assert reused_second is True
    assert wired == {"news": 1, "llm": 1}  # no extra calls
    assert second.model_dump() == first.model_dump()
    assert second.generated_at == first.generated_at


def test_hit_does_not_rewrite_file(wired, monkeypatch) -> None:
    _resolve()  # populate

    def _boom(_narrative):
        raise AssertionError("save_market_narrative must not run on a cache hit")

    monkeypatch.setattr(mn, "save_market_narrative", _boom)
    _, reused = _resolve()
    assert reused is True


def test_corrupt_file_regenerates(wired) -> None:
    _resolve()
    mn.MARKET_NARRATIVE_JSON.write_text("not json", encoding="utf-8")
    _, reused = _resolve()
    assert reused is False


def test_digestless_file_regenerates(wired) -> None:
    _resolve()
    payload = json.loads(mn.MARKET_NARRATIVE_JSON.read_text(encoding="utf-8"))
    payload.pop("input_digest", None)
    mn.MARKET_NARRATIVE_JSON.write_text(json.dumps(payload), encoding="utf-8")
    _, reused = _resolve()
    assert reused is False


def test_over_age_file_regenerates(wired) -> None:
    _resolve()
    payload = json.loads(mn.MARKET_NARRATIVE_JSON.read_text(encoding="utf-8"))
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    payload["generated_at"] = stale.isoformat(timespec="seconds")
    mn.MARKET_NARRATIVE_JSON.write_text(json.dumps(payload), encoding="utf-8")
    _, reused = _resolve()
    assert reused is False


def test_from_fallback_file_regenerates(wired, monkeypatch) -> None:
    monkeypatch.setattr(mn, "build_llm_narrative", lambda *a, **k: None)
    first, reused_first = _resolve()
    assert reused_first is False
    assert first.from_fallback is True

    monkeypatch.setattr(
        mn,
        "build_llm_narrative",
        lambda *a, **k: MarketNarrative(
            headline="LLM headline",
            bullets=["b1"],
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    _, reused_second = _resolve()
    assert reused_second is False  # fallback never reused


def test_non_finite_float_input_persists_but_never_reused(wired, monkeypatch) -> None:
    s, w, c, b, g = _inputs()
    bad_breadth = b.model_copy(update={"pct_above_200dma": math.inf})

    narrative, reused = mn.resolve_market_narrative(
        s, w, c, bad_breadth, g, _AnthropicStub(), None
    )
    assert reused is False
    assert narrative.input_digest == ""
    assert mn.MARKET_NARRATIVE_JSON.exists()

    _, reused_again = mn.resolve_market_narrative(
        s, w, c, bad_breadth, g, _AnthropicStub(), None
    )
    assert reused_again is False
