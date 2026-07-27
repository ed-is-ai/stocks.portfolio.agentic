"""Claude's raw structured output before the hallucination guard runs.

Deliberately distinct from ``MarketNarrative``: each bullet carries the
domains Claude claims to have drawn it from (``cited_domains``), so
``app.agents.scanner.narrative_guard.validate_against_context`` can check
those citations mechanically against the ``NewsContext`` actually supplied,
rather than trying to free-parse prose for hallucinated outlet names. Only
bullets (and a headline) that pass the guard are copied into the flat
``bullets: list[str]`` that ``MarketNarrative`` renders.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LlmNarrativeBullet(BaseModel):
    """One bullet from Claude, with the domains it claims to be grounded in.

    ``cited_domains`` is empty for bullets that don't rely on a specific news
    item (e.g. a pure sector-allocation or market-cycle observation) — those
    still pass the guard, since there's nothing to hallucinate.
    """

    text: str
    cited_domains: list[str] = Field(default_factory=list)


class LlmNarrativeDraft(BaseModel):
    """Claude's unvalidated narrative draft, parsed from the structured output."""

    headline: str
    bullets: list[LlmNarrativeBullet] = Field(default_factory=list)
