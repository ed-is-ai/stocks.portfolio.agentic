"""Headline-level news/macro context gathered once per scan run.

Deliberately shallow: only headline, domain, url, sentiment, and seen-date
are captured — never article bodies. Phase 3's Claude summariser consumes
``NewsContext`` to populate ``MarketNarrative.sources`` and write prose;
Phase 2 only gathers and shapes the raw material.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

_MAX_ITEMS = 15


class NewsItem(BaseModel):
    """A single headline-level news item from one of the news feeds."""

    title: str
    domain: str
    url: str
    sentiment: float | None = None
    seen_date: datetime | None = None
    source_feed: str  # "alpha_vantage" | "gdelt"


class NewsContext(BaseModel):
    """The full set of news/macro context gathered for one scan run.

    ``degraded`` is True when both feeds failed or returned nothing, signalling
    to Phase 3 that it should fall back to evergreen (non-news-grounded) prose
    rather than fabricate context from an empty item list.
    """

    items: list[NewsItem] = Field(default_factory=list, max_length=_MAX_ITEMS)
    sector_queries: list[str] = Field(default_factory=list)
    macro_query: str = ""
    fetched_at: datetime
    degraded: bool = False
