"""Market-narrative blurb: Claude summariser (#109 Phase 3) with a
deterministic evergreen fallback (#109 Phase 1).

``build_deterministic_narrative`` builds a plain-English ``MarketNarrative``
from sector-allocation deltas, portfolio sector weights, and the static FOMC
market-cycle context — no LLM and no network involved. ``build_llm_narrative``
builds the *same* ``MarketNarrative`` shape from a Claude Sonnet 5 summary of
those same structured inputs plus headline-level news context, run through
the hallucination guard (``app.agents.scanner.narrative_guard``). Callers
(``AlertAgent.send_summary_email``, the watchlist banner) only ever render a
``MarketNarrative`` and don't need to change based on which builder produced it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.agents.scanner.market_cycle import (
    PHASE_MID_CYCLE,
    PHASE_POST_FOMC,
    PHASE_PRE_FOMC_BLACKOUT,
)
from app.agents.scanner.narrative_guard import validate_against_context
from app.core.config import MARKET_NARRATIVE_JSON
from app.integrations.anthropic_client import AnthropicNarrativeClient
from app.schemas.market_breadth import MarketBreadth
from app.schemas.market_cycle import MarketCycleContext
from app.schemas.market_narrative import MarketNarrative, MarketNarrativeSource
from app.schemas.news_context import NewsContext
from app.schemas.sector_allocation import (
    CongressionalBuy,
    PortfolioSectorWeight,
    SectorAllocationSnapshot,
)

NOT_ADVICE_NOTE = (
    "Informational only, not financial advice. Sector prevalence reflects "
    "scanner candidates, not a recommendation to buy or sell."
)

_PHASE_LABELS = {
    PHASE_PRE_FOMC_BLACKOUT: "in the pre-FOMC blackout window",
    PHASE_POST_FOMC: "just past an FOMC decision",
    PHASE_MID_CYCLE: "mid-cycle between FOMC meetings",
}

_TOP_N = 3


def _pct(share: float) -> str:
    """Format a 0..1 fraction as a whole-number percentage string."""
    return f"{share * 100:.0f}%"


def _prevalence_bullet(snapshot: SectorAllocationSnapshot) -> str | None:
    """Return the "most-prevalent sectors" bullet, or None if no candidates."""
    top_shares = snapshot.shares[:_TOP_N]
    if not top_shares:
        return None
    leaders = ", ".join(
        f"{s.sector} ({_pct(s.count_share)}, {_pct(s.strong_share)} at 7/10+)"
        for s in top_shares
    )
    return f"Most-prevalent scan sectors this run: {leaders}."


def _delta_bullets(snapshot: SectorAllocationSnapshot) -> list[str]:
    """Return gaining/losing week-on-week bullets, or a first-run note."""
    if not snapshot.deltas:
        return [
            "No prior ~7-day baseline yet — this is the first "
            "sector-prevalence snapshot."
        ]
    window = snapshot.lookback_days
    suffix = f" over the past ~{window}d" if window is not None else ""
    bullets: list[str] = []
    gainers = [d for d in snapshot.deltas if d.delta > 0][:_TOP_N]
    losers = [d for d in snapshot.deltas if d.delta < 0][:_TOP_N]
    if gainers:
        bullets.append(
            f"Gaining prevalence{suffix}: "
            + ", ".join(f"{d.sector} (+{_pct(d.delta)})" for d in gainers)
            + "."
        )
    if losers:
        bullets.append(
            f"Losing prevalence{suffix}: "
            + ", ".join(f"{d.sector} ({_pct(d.delta)})" for d in losers)
            + "."
        )
    return bullets


def _myb_bullet(snapshot: SectorAllocationSnapshot) -> str | None:
    """Return the "sectors with most multi-year breakouts" bullet, or None."""
    ranked = sorted(
        (s for s in snapshot.shares if s.myb_count > 0),
        key=lambda s: -s.myb_count,
    )[:_TOP_N]
    if not ranked:
        return None
    return (
        "Most multi-year breakouts by sector: "
        + ", ".join(f"{s.sector} ({s.myb_count})" for s in ranked)
        + "."
    )


def _breadth_bullet(breadth: MarketBreadth | None) -> str | None:
    """Return the S&P 500 market-breadth bullet, or None when unavailable."""
    if breadth is None:
        return None
    trend = (
        "rising"
        if breadth.trend_rising
        else "falling"
        if breadth.trend_rising is False
        else "flat"
    )
    divergence = " — breadth-divergence flag set" if breadth.bearish_signal else ""
    return (
        f"S&P 500 breadth: {breadth.pct_above_200dma:.0f}% of members above "
        f"their 200DMA ({trend}){divergence}."
    )


def _congress_bullet(congress: list[CongressionalBuy]) -> str | None:
    """Return the "most heavily bought by lawmakers" bullet, or None if empty."""
    if not congress:
        return None
    names = ", ".join(f"{c.ticker} (+{c.congress_net})" for c in congress[:_TOP_N])
    return f"Most heavily net-bought by Congress/Senate: {names}."


def _portfolio_bullet(weights: list[PortfolioSectorWeight]) -> str | None:
    """Return the "current holdings concentrated in" bullet, or None if empty."""
    top_holdings = weights[:_TOP_N]
    if not top_holdings:
        return None
    return (
        "Current holdings concentrated in: "
        + ", ".join(f"{w.sector} ({_pct(w.value_share)})" for w in top_holdings)
        + "."
    )


def _cycle_bullet(cycle: MarketCycleContext) -> str:
    """Return the FOMC market-cycle context bullet."""
    phase_label = _PHASE_LABELS.get(cycle.phase, cycle.phase)
    bits = [f"Market is {phase_label}"]
    if cycle.days_since_last_decision is not None:
        bits.append(f"{cycle.days_since_last_decision}d since the last FOMC decision")
    if cycle.days_to_next_meeting is not None:
        bits.append(f"{cycle.days_to_next_meeting}d to the next meeting")
    return ", ".join(bits) + "."


def build_deterministic_narrative(
    scan_snapshot: SectorAllocationSnapshot,
    portfolio_weights: list[PortfolioSectorWeight],
    cycle: MarketCycleContext,
    breadth: MarketBreadth | None = None,
    congress: list[CongressionalBuy] | None = None,
) -> MarketNarrative:
    """Build the deterministic sector-allocation + market-cycle blurb.

    Fully rule-based: no API keys, no network, no LLM. This is the evergreen
    fallback rendered whenever a later, Claude-generated narrative isn't
    available. Breadth and congressional signals are folded in when supplied.
    """
    bullets: list[str] = []
    prevalence = _prevalence_bullet(scan_snapshot)
    if prevalence:
        bullets.append(prevalence)
    bullets.extend(_delta_bullets(scan_snapshot))
    for optional in (
        _myb_bullet(scan_snapshot),
        _breadth_bullet(breadth),
        _congress_bullet(congress or []),
        _portfolio_bullet(portfolio_weights),
    ):
        if optional:
            bullets.append(optional)
    bullets.append(_cycle_bullet(cycle))

    headline = (
        f"Scanner sector mix: {scan_snapshot.shares[0].sector} leads"
        if scan_snapshot.shares
        else "Sector-allocation snapshot"
    )

    return MarketNarrative(
        headline=headline,
        bullets=bullets,
        sources=[],
        not_advice=NOT_ADVICE_NOTE,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _sources_from_domains(
    cited_domains: list[str], news_context: NewsContext
) -> list[MarketNarrativeSource]:
    """Build the digest footnote list from the news items actually cited.

    One source per cited domain (first matching item wins), in the order
    Claude cited them — not every fed item, only the ones the guard
    confirmed were actually drawn on.
    """
    by_domain: dict[str, MarketNarrativeSource] = {}
    for item in news_context.items:
        domain = item.domain.strip().lower()
        domain = domain[4:] if domain.startswith("www.") else domain
        by_domain.setdefault(
            domain, MarketNarrativeSource(label=item.domain, url=item.url)
        )
    return [by_domain[d] for d in cited_domains if d in by_domain]


def build_llm_narrative(
    scan_snapshot: SectorAllocationSnapshot,
    portfolio_weights: list[PortfolioSectorWeight],
    cycle: MarketCycleContext,
    news_context: NewsContext,
    anthropic_client: AnthropicNarrativeClient,
    breadth: MarketBreadth | None = None,
    congress: list[CongressionalBuy] | None = None,
) -> MarketNarrative | None:
    """Attempt a Claude-generated ``MarketNarrative``; ``None`` on any degraded path.

    Returns ``None`` — signalling the caller to fall back to
    ``build_deterministic_narrative`` — when: the client has no API key
    configured, the news context came back degraded or empty (nothing to
    ground a summary in), the API call itself fails or is refused, or the
    hallucination guard rejects the draft outright (bad headline) or leaves
    no bullets standing. This must never raise: every failure mode here is
    absorbed into a ``None`` return, mirroring
    ``AnalystAgent.get_llm_client()``'s graceful-skip pattern.
    """
    if not anthropic_client.enabled:
        return None
    if news_context.degraded or not news_context.items:
        return None

    draft = anthropic_client.generate_market_narrative(
        scan_snapshot, portfolio_weights, cycle, news_context, breadth, congress
    )
    if draft is None:
        return None

    result = validate_against_context(draft, news_context)
    if not result.valid or not result.kept_bullets:
        return None

    return MarketNarrative(
        headline=draft.headline,
        bullets=result.kept_bullets,
        sources=_sources_from_domains(result.cited_domains, news_context),
        not_advice=NOT_ADVICE_NOTE,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def save_market_narrative(narrative: MarketNarrative) -> None:
    """Persist *narrative* so the web banner and digest re-renders can reuse it.

    A simple whole-file overwrite is enough — only the latest run's narrative
    is ever needed (unlike the sector/scan history, which tracks deltas
    across runs).
    """
    MARKET_NARRATIVE_JSON.write_text(
        json.dumps(narrative.model_dump(mode="json"), indent=2), encoding="utf-8"
    )


def load_market_narrative() -> MarketNarrative | None:
    """Return the persisted narrative from the latest run, or None if absent."""
    try:
        payload = json.loads(MARKET_NARRATIVE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return MarketNarrative.model_validate(payload)
    except Exception:
        return None
