"""The rendered market-narrative object shown in the digest email and web UI.

Deliberately provider-agnostic: Phase 1 fills this from deterministic sector
and market-cycle computations (see ``app.agents.scanner.market_narrative``);
a later phase can populate the same shape from a Claude-generated summary
without any caller needing to change (``AlertAgent.send_summary_email`` and
the watchlist banner both just render whatever ``MarketNarrative`` they are
given).
"""

from __future__ import annotations

from pydantic import BaseModel


class MarketNarrativeSource(BaseModel):
    """A citation backing a narrative bullet (empty for deterministic output)."""

    label: str
    url: str | None = None


class MarketNarrative(BaseModel):
    """A short, structured market-context blurb for the digest/web header."""

    headline: str
    bullets: list[str] = []
    sources: list[MarketNarrativeSource] = []
    not_advice: str = "Informational only — not financial advice."
    generated_at: str = ""  # ISO timestamp; set by the builder
