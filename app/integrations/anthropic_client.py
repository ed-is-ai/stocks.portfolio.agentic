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
from app.schemas.market_cycle import MarketCycleContext
from app.schemas.news_context import NewsContext
from app.schemas.sector_allocation import (
    PortfolioSectorWeight,
    SectorAllocationSnapshot,
)

_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 1024
_TOP_N = 3

_SYSTEM_PROMPT = (
    "You are writing a short, factual market-context blurb for a personal "
    "stock-portfolio digest email. You will be given: (1) how the scanner's "
    "sector mix has shifted run-over-run, (2) the user's current portfolio "
    "sector weights, (3) where we are in the FOMC meeting cycle, and (4) a "
    "handful of recent news headlines with their source domain and URL. "
    "Write one headline and a few bullets covering: how the sector "
    "allocation has changed, how that reads against the current point in "
    "the market/financial cycle, and any world-events context — but ONLY "
    "using the headlines you were given. Do not name any news outlet, "
    "publication, event, or claim that is not directly present in the "
    "supplied headlines — if you have no relevant headline for a point, "
    "omit it rather than filling in from general knowledge. For every "
    "bullet that draws on a headline, list the exact domain string of that "
    "headline in cited_domains; leave cited_domains empty for bullets based "
    "only on the sector/cycle data. This is informational market context "
    "only, not financial advice or a recommendation to buy or sell — do not "
    "phrase anything as a recommendation."
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
) -> str:
    """Render the structured context into a plain-text user prompt."""
    lines: list[str] = []

    lines.append(f"As of: {scan_snapshot.as_of}")
    lines.append("Sector prevalence this run (share of scanned candidates):")
    for share in scan_snapshot.shares[:_TOP_N]:
        lines.append(f"- {share.sector}: {_pct(share.count_share)}")
    if scan_snapshot.deltas:
        lines.append("Run-over-run sector-prevalence change:")
        for delta in scan_snapshot.deltas[:_TOP_N]:
            lines.append(f"- {delta.sector}: {_pct(delta.delta)} change")
    else:
        lines.append("No prior run to compare against yet.")

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
                            scan_snapshot, portfolio_weights, cycle, news_context
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
