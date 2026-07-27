"""Gathers fresh headline-level news/macro context for the narrative layer.

Pure orchestration over two injected feeds (Alpha Vantage NEWS_SENTIMENT and
GDELT DOC 2.0): builds sector + macro queries from the run's top-prevalence
sectors, fetches both feeds, unions and de-duplicates by URL, ranks by
recency, and caps the result at a digest-sized handful of items. No LLM is
involved here — Phase 3 consumes the resulting ``NewsContext`` to populate
``MarketNarrative.sources`` and generate prose.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.integrations import gdelt
from app.integrations.alpha_vantage import SECTOR_TO_AV_TOPIC, AlphaVantageClient
from app.schemas.news_context import NewsContext, NewsItem

_MAX_ITEMS = 15
_MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)


def _av_topics_for_sectors(sectors: list[str]) -> list[str]:
    """Map top-prevalence sectors to Alpha Vantage's ``topics`` vocabulary.

    Sectors with no mapping (see ``SECTOR_TO_AV_TOPIC``) are dropped rather
    than guessed at; duplicates are collapsed while preserving order.
    """
    topics: list[str] = []
    for sector in sectors:
        topic = SECTOR_TO_AV_TOPIC.get(sector)
        if topic and topic not in topics:
            topics.append(topic)
    return topics


def _parse_av_time(raw: str | None) -> datetime | None:
    """Parse Alpha Vantage's ``YYYYMMDDTHHMMSS`` time_published field."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _av_feed_to_item(raw: dict[str, Any]) -> NewsItem | None:
    """Map one raw Alpha Vantage NEWS_SENTIMENT feed entry to a ``NewsItem``."""
    url = raw.get("url")
    title = raw.get("title")
    if not url or not title:
        return None
    sentiment_raw = raw.get("overall_sentiment_score")
    try:
        sentiment = float(sentiment_raw) if sentiment_raw is not None else None
    except (TypeError, ValueError):
        sentiment = None
    return NewsItem(
        title=title,
        domain=raw.get("source_domain") or raw.get("source") or "",
        url=url,
        sentiment=sentiment,
        seen_date=_parse_av_time(raw.get("time_published")),
        source_feed="alpha_vantage",
    )


def _fetch_alpha_vantage_items(
    av_client: AlphaVantageClient | None, top_sectors: list[str]
) -> list[NewsItem]:
    """Fetch and parse AV NEWS_SENTIMENT items for *top_sectors*.

    Returns ``[]`` when *av_client* is ``None`` or disabled (no API key) —
    the caller treats this the same as any other empty feed.
    """
    if av_client is None or not av_client.enabled:
        return []
    topics = _av_topics_for_sectors(top_sectors)
    raw_feed = av_client.get_news_sentiment(topics=topics or None)
    items = [_av_feed_to_item(entry) for entry in raw_feed]
    return [item for item in items if item is not None]


def _fetch_gdelt_items(top_sectors: list[str]) -> list[NewsItem]:
    """Fetch and parse GDELT items for *top_sectors* plus the macro query."""
    raw_items = gdelt.fetch_news_items(top_sectors)
    return [NewsItem(**raw) for raw in raw_items]


def _dedupe_by_url(items: list[NewsItem]) -> list[NewsItem]:
    """Return *items* with later duplicate URLs dropped, order preserved."""
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        deduped.append(item)
    return deduped


def _rank_key(item: NewsItem) -> tuple[datetime, float]:
    """Sort key: most recent first, ties broken by |sentiment| magnitude."""
    seen_date = item.seen_date or _MIN_DATETIME
    relevance = abs(item.sentiment) if item.sentiment is not None else 0.0
    return (seen_date, relevance)


def gather_news_context(
    top_sectors: list[str], av_client: AlphaVantageClient | None
) -> NewsContext:
    """Gather one run's worth of news/macro context from both feeds.

    Fetches Alpha Vantage NEWS_SENTIMENT (topics derived from *top_sectors*)
    and GDELT DOC 2.0 (sector + always-on macro query), unions the results,
    de-duplicates by URL, ranks by recency (ties broken by sentiment
    magnitude), and caps at ``_MAX_ITEMS``. ``degraded`` is set when both
    feeds returned nothing, signalling Phase 3 to fall back to evergreen
    prose rather than summarise an empty context.
    """
    av_items = _fetch_alpha_vantage_items(av_client, top_sectors)
    gdelt_items = _fetch_gdelt_items(top_sectors)

    combined = _dedupe_by_url(av_items + gdelt_items)
    combined.sort(key=_rank_key, reverse=True)
    capped = combined[:_MAX_ITEMS]

    return NewsContext(
        items=capped,
        sector_queries=list(top_sectors),
        macro_query=gdelt.MACRO_QUERY,
        fetched_at=datetime.now(timezone.utc),
        degraded=not av_items and not gdelt_items,
    )
