"""Hallucination guard for Claude-generated market narratives (#109 Phase 3).

Claude is fed only headline-level ``NewsItem`` records (title, domain, url,
sentiment — never article bodies) and asked to tag which domains ground each
bullet it writes. This module checks those claims mechanically: a bullet
whose ``cited_domains`` names something outside the fed set, or whose text
mentions a well-known news outlet absent from the fed set, is dropped rather
than rendered. A bad headline rejects the whole draft — it's prominent
enough that dropping individual bullets can't fix it.

Pure and side-effect-free so it can be unit tested without ever calling the
Anthropic API — see ``tests/test_narrative_guard.py``.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.schemas.llm_narrative import LlmNarrativeDraft
from app.schemas.news_context import NewsContext

# Well-known financial/news outlets Claude might name from general knowledge
# (rather than from the fed context), mapped to the domain that would have
# to appear in the fed NewsContext to justify the mention. This is a safety
# net alongside the schema-enforced `cited_domains` tagging — it catches an
# outlet name embedded in free text without a matching citation.
_KNOWN_OUTLET_DOMAINS: dict[str, str] = {
    "reuters": "reuters.com",
    "bloomberg": "bloomberg.com",
    "cnbc": "cnbc.com",
    "wall street journal": "wsj.com",
    "wsj": "wsj.com",
    "financial times": "ft.com",
    "marketwatch": "marketwatch.com",
    "barron's": "barrons.com",
    "barrons": "barrons.com",
    "yahoo finance": "yahoo.com",
    "associated press": "apnews.com",
    "ap news": "apnews.com",
    "the economist": "economist.com",
    "forbes": "forbes.com",
    "business insider": "businessinsider.com",
    "axios": "axios.com",
    "seeking alpha": "seekingalpha.com",
    "benzinga": "benzinga.com",
    "zacks": "zacks.com",
    "motley fool": "fool.com",
    "npr": "npr.org",
    "the guardian": "theguardian.com",
    "new york times": "nytimes.com",
    "washington post": "washingtonpost.com",
}


class ValidationResult(BaseModel):
    """Outcome of running a Claude narrative draft through the guard.

    ``valid=False`` means reject the whole draft (headline problem) — the
    caller should fall back to the deterministic narrative entirely. When
    ``valid=True``, ``kept_bullets``/``dropped_bullets`` partition the
    draft's bullets and ``cited_domains`` lists the (deduplicated) domains
    actually grounding the kept bullets, for building ``MarketNarrative.sources``.
    """

    valid: bool
    kept_bullets: list[str] = Field(default_factory=list)
    dropped_bullets: list[str] = Field(default_factory=list)
    cited_domains: list[str] = Field(default_factory=list)


def _domain_root(domain: str) -> str:
    """Normalise *domain* for comparison (lowercase, strip ``www.``)."""
    normalised = domain.strip().lower()
    return normalised[4:] if normalised.startswith("www.") else normalised


def _mentions_unfed_outlet(text: str, fed_domain_roots: set[str]) -> bool:
    """Return True if *text* names a well-known outlet absent from the feed."""
    lowered = text.lower()
    for name, domain in _KNOWN_OUTLET_DOMAINS.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered) and domain not in (
            fed_domain_roots
        ):
            return True
    return False


def _cited_domains_ok(cited: list[str], fed_domain_roots: set[str]) -> bool:
    """Return True only if every domain a bullet cites was actually fed."""
    return all(_domain_root(c) in fed_domain_roots for c in cited if c.strip())


def validate_against_context(
    draft: LlmNarrativeDraft, news_context: NewsContext
) -> ValidationResult:
    """Ground *draft* against *news_context*, dropping unsupported claims.

    A clean draft (no citations, or citations/mentions matching the fed
    domains) passes through unchanged. Any bullet citing an un-fed domain, or
    naming a well-known outlet absent from the fed set, is dropped. A
    headline with the same problem rejects the entire draft.
    """
    fed_domain_roots = {
        _domain_root(item.domain) for item in news_context.items if item.domain
    }

    if _mentions_unfed_outlet(draft.headline, fed_domain_roots):
        return ValidationResult(valid=False)

    kept: list[str] = []
    dropped: list[str] = []
    cited_domains: list[str] = []
    for bullet in draft.bullets:
        grounded = _cited_domains_ok(
            bullet.cited_domains, fed_domain_roots
        ) and not _mentions_unfed_outlet(bullet.text, fed_domain_roots)
        if not grounded:
            dropped.append(bullet.text)
            continue
        kept.append(bullet.text)
        for cited in bullet.cited_domains:
            root = _domain_root(cited)
            if root and root not in cited_domains:
                cited_domains.append(root)

    return ValidationResult(
        valid=True,
        kept_bullets=kept,
        dropped_bullets=dropped,
        cited_domains=cited_domains,
    )
