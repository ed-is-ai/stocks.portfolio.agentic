"""Claude Sonnet 5 client for the market-narrative summariser (#109 Phase 3).

Mirrors ``AnalystAgent.get_llm_client()``'s graceful-skip pattern: absent an
``ANTHROPIC_API_KEY``, or on any SDK/network/parsing error, every method here
returns ``None`` rather than raising — the caller always has a deterministic
fallback (``build_deterministic_narrative``) and the pipeline must never fail
because Claude is unset, rate-limited, or unreachable.

One call per run, no thinking (this is a short structured-summary task, not
a reasoning-heavy one), and a JSON-schema-constrained response so parsing is
robust without a tool-use round trip.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.schemas.llm_narrative import LlmNarrativeDraft
from app.schemas.market_breadth import MarketBreadth
from app.schemas.market_cycle import MarketCycleContext
from app.schemas.news_context import NewsContext
from app.schemas.sector_allocation import (
    CongressionalBuy,
    PortfolioSectorWeight,
    SectorAllocationSnapshot,
)

_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 1536
_TOP_N = 4

_SYSTEM_PROMPT = (
    "You are writing a short, factual market-context blurb for a personal "
    "stock-portfolio digest email. You are given deterministic, trustworthy "
    "figures — treat them as facts you may state directly: (1) where is the momentum? sector "
    "prevalence in this scan run, including each sector's share of the whole "
    "scanned universe that is high-conviction (scoring 7/10 or higher) ordered by high conviction %, and "
    "how the sector mix has shifted week-on-week over the stated lookback "
    "window; (2) which sectors show the most multi-year base breakouts this "
    "run; (3) the user's current portfolio sector weights; (4) an S&P 500 "
    "market-breadth reading — the percentage of members above their 200-day "
    "average — a whole-market participation gauge the scan's filtered "
    "candidate list cannot provide; (5) which individual stocks are being "
    "most heavily net-bought by US Congress / Senate filers; (6) where we sit "
    "in the FOMC meeting cycle; and (7) a handful of recent news headlines "
    "with their source domain and URL.\n\n"
    "Write one headline and a few bullets that: describe the sector "
    "allocation and its week-on-week shift, leading with sectors most "
    "prevalent among high-conviction (7/10+) names; read the market's likely "
    "cycle position and risk posture from the mix — e.g. whether leadership "
    "looks defensive / late-cycle (utilities, staples, healthcare, REITs) or "
    "risk-on / early-to-mid-cycle (technology, discretionary, industrials) — "
    "framed against breadth and the FOMC position, hedged as 'likely' or "
    "'consistent with', never as certainty; note whether market breadth is "
    "broad or narrow / diverging and what that implies alongside the tilt; "
    "call out which sectors are seeing the most multi-year breakouts; flag "
    "the stocks lawmakers are most heavily buying, if any; and weave in "
    "relevant world-events context to help explain the rotation — but ONLY "
    "using the headlines you were given.\n\n"
    "Do not name any news outlet, publication, event, or news claim that is "
    "not directly present in the supplied headlines; if you have no relevant "
    "headline for a point, omit that world-events angle rather than filling "
    "it in from general knowledge. The sector, breadth, congressional, cycle "
    "and multi-year-breakout figures above are supplied facts you may state "
    "directly. For every bullet that draws on a news headline, list the exact "
    "domain string(s) of that headline in cited_domains; leave cited_domains "
    "empty for bullets based only on the supplied figures.  End with a short, plain-English summary of the market's likely short-term direction and risk posture, framed as 'likely' or 'consistent with', never as certainty."
)

_NARRATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "cited_domains": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "cited_domains"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "bullets"],
    "additionalProperties": False,
}


def _pct(share: float) -> str:
    """Format a 0..1 fraction as a whole-number percentage string."""
    return f"{share * 100:.0f}%"


def _build_user_prompt(
    scan_snapshot: SectorAllocationSnapshot,
    portfolio_weights: list[PortfolioSectorWeight],
    cycle: MarketCycleContext,
    news_context: NewsContext,
    breadth: MarketBreadth | None,
    congress: list[CongressionalBuy],
) -> str:
    """Render the structured context into a plain-text user prompt."""
    lines: list[str] = []

    lines.append(f"As of: {scan_snapshot.as_of}")
    lines.append(
        "Sector prevalence this run "
        "(share of candidates | high-conviction 7/10+ share of universe | "
        "multi-year breakouts):"
    )
    for share in scan_snapshot.shares[:_TOP_N]:
        lines.append(
            f"- {share.sector}: {_pct(share.count_share)} | "
            f"{_pct(share.strong_share)} 7/10+ | {share.myb_count} MYB"
        )

    if scan_snapshot.deltas:
        window = scan_snapshot.lookback_days
        label = (
            f"Week-on-week sector-prevalence shift (over ~{window} days):"
            if window is not None
            else "Sector-prevalence shift vs. the prior run:"
        )
        lines.append(label)
        for delta in scan_snapshot.deltas[:_TOP_N]:
            lines.append(f"- {delta.sector}: {_pct(delta.delta)} change")
    else:
        lines.append("No prior ~7-day baseline yet (first sector snapshot).")

    top_myb = sorted(
        (s for s in scan_snapshot.shares if s.myb_count > 0),
        key=lambda s: -s.myb_count,
    )[:_TOP_N]
    if top_myb:
        lines.append("Sectors with the most multi-year breakouts this run:")
        for share in top_myb:
            lines.append(f"- {share.sector}: {share.myb_count}")

    if breadth is not None:
        trend = (
            "rising"
            if breadth.trend_rising
            else "falling"
            if breadth.trend_rising is False
            else "flat/unknown"
        )
        stale = "" if breadth.is_fresh else " (stale)"
        bearish = "; bearish-divergence flag set" if breadth.bearish_signal else ""
        lines.append(
            f"S&P 500 market breadth: {breadth.pct_above_200dma:.0f}% of members "
            f"above their 200DMA, trend {trend}{bearish} "
            f"(as of {breadth.as_of}{stale})."
        )

    if congress:
        lines.append(
            "Stocks most heavily net-bought by Congress/Senate "
            "(ticker | sector | congress net | senate net):"
        )
        for buy in congress:
            lines.append(
                f"- {buy.ticker} | {buy.sector} | {buy.congress_net:+d} | "
                f"{buy.senate_net:+d}"
            )

    if portfolio_weights:
        lines.append("Current portfolio sector weights:")
        for weight in portfolio_weights[:_TOP_N]:
            lines.append(f"- {weight.sector}: {_pct(weight.value_share)}")

    lines.append(f"Market cycle phase: {cycle.phase}")
    if cycle.days_since_last_decision is not None:
        lines.append(f"Days since last FOMC decision: {cycle.days_since_last_decision}")
    if cycle.days_to_next_meeting is not None:
        lines.append(f"Days to next FOMC meeting: {cycle.days_to_next_meeting}")

    lines.append("Recent headlines (title | domain | url | sentiment):")
    for item in news_context.items:
        sentiment = f"{item.sentiment:.2f}" if item.sentiment is not None else "n/a"
        lines.append(f"- {item.title} | {item.domain} | {item.url} | {sentiment}")

    return "\n".join(lines)


class AnthropicNarrativeClient:
    """Thin wrapper over the Anthropic Messages API for narrative summarisation.

    Gated on ``ANTHROPIC_API_KEY`` (env var, or an explicit override for
    tests). ``enabled`` tells the caller whether it's worth even gathering
    news context; ``generate_market_narrative`` does the one API call and
    returns ``None`` on any failure so the pipeline degrades silently to the
    deterministic narrative.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")

    @property
    def enabled(self) -> bool:
        """Return True when an API key is configured."""
        return bool(self.api_key)

    def generate_market_narrative(
        self,
        scan_snapshot: SectorAllocationSnapshot,
        portfolio_weights: list[PortfolioSectorWeight],
        cycle: MarketCycleContext,
        news_context: NewsContext,
        breadth: MarketBreadth | None = None,
        congress: list[CongressionalBuy] | None = None,
    ) -> LlmNarrativeDraft | None:
        """Ask Claude for a narrative draft, or ``None`` on any failure.

        Never raises: an unset key, an import error, an API error (including
        a safety refusal), or a response that doesn't parse as the expected
        JSON shape all result in ``None`` rather than propagating — the
        caller always has ``build_deterministic_narrative`` to fall back to.
        """
        if not self.enabled:
            return None
        try:
            import anthropic
        except ImportError:
            return None

        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                thinking={"type": "disabled"},
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": _build_user_prompt(
                            scan_snapshot,
                            portfolio_weights,
                            cycle,
                            news_context,
                            breadth,
                            congress or [],
                        ),
                    }
                ],
                output_config={
                    "format": {"type": "json_schema", "schema": _NARRATIVE_SCHEMA}
                },
            )
        except Exception:
            return None

        if response.stop_reason != "end_turn":
            # "refusal", "max_tokens", etc. — no reliable structured output.
            return None
        try:
            text = next(b.text for b in response.content if b.type == "text")
            payload = json.loads(text)
            return LlmNarrativeDraft.model_validate(payload)
        except Exception:
            return None
