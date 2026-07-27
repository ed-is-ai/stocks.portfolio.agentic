"""GDELT DOC 2.0 API client — keyless global news search.

Complements Alpha Vantage's NEWS_SENTIMENT feed with a second, independent
news source for the market-narrative layer (#109). Keyless HTTP GET against
the public DOC 2.0 endpoint; no sentiment is returned by GDELT, so parsed
``NewsItem``s always have ``sentiment=None``.

The endpoint can be slow (~15s) and occasionally flaky, so requests are
retried with exponential backoff via tenacity (mirroring
``app.agents.scanner.scanner_agent._fetch_ticker_info``); a fully exhausted
retry returns ``[]`` rather than raising, so callers can simply treat GDELT
as "best effort" and mark the overall news context degraded when both feeds
come back empty.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_TIMESPAN = "24H"
_MAX_RECORDS = 75
_TIMEOUT_SECONDS = 20
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2.0

# Always-on macro query: rates / USD / inflation context, independent of the
# run's top sectors.
MACRO_QUERY = (
    '(inflation OR "interest rates" OR "Federal Reserve" OR "US dollar") '
    "sourcelang:english"
)


def build_sector_query(sectors: list[str]) -> str:
    """Return a GDELT query string for the given top-prevalence sectors.

    Sectors are OR'd together and the result is restricted to English-language
    sources. Returns an empty string when *sectors* is empty (caller should
    skip the sector fetch in that case).
    """
    if not sectors:
        return ""
    sector_clause = " OR ".join(f'"{sector}"' for sector in sectors)
    return f"({sector_clause}) sourcelang:english"


@retry(
    retry=retry_if_exception_type(Exception),
    wait=wait_exponential(multiplier=_RETRY_BACKOFF_SECONDS),
    stop=stop_after_attempt(_RETRY_ATTEMPTS),
    retry_error_callback=lambda _state: [],
)
def _fetch(query: str) -> list[dict[str, Any]]:
    """Issue one GDELT DOC 2.0 query and return the raw ``articles`` array.

    Retries on any exception (network error, timeout, bad JSON); after the
    final attempt is exhausted, returns ``[]`` instead of raising.
    """
    resp = requests.get(
        _BASE_URL,
        params={
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": _TIMESPAN,
            "maxrecords": str(_MAX_RECORDS),
            "sort": "datedesc",
        },
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data = resp.json()
    articles = data.get("articles", [])
    return articles if isinstance(articles, list) else []


def _parse_seendate(raw: str | None) -> datetime | None:
    """Parse GDELT's ``YYYYMMDDTHHMMSSZ`` seendate into a UTC datetime."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _article_to_item(article: dict[str, Any]) -> dict[str, Any] | None:
    """Map one raw GDELT article dict to ``NewsItem`` constructor kwargs."""
    url = article.get("url")
    title = article.get("title")
    if not url or not title:
        return None
    return {
        "title": title,
        "domain": article.get("domain") or "",
        "url": url,
        "sentiment": None,
        "seen_date": _parse_seendate(article.get("seendate")),
        "source_feed": "gdelt",
    }


def fetch_news_items(sectors: list[str]) -> list[dict[str, Any]]:
    """Fetch and parse GDELT articles for *sectors* plus the macro query.

    Issues one query for the sector keywords (skipped when *sectors* is
    empty) and one always-on macro query, unions the parsed results, and
    returns a list of dicts shaped for ``NewsItem(**item)``. Any total
    failure on a leg (retries exhausted) contributes no items rather than
    raising.
    """
    raw_articles: list[dict[str, Any]] = []
    sector_query = build_sector_query(sectors)
    if sector_query:
        raw_articles.extend(_fetch(sector_query))
    raw_articles.extend(_fetch(MACRO_QUERY))

    items: list[dict[str, Any]] = []
    for article in raw_articles:
        item = _article_to_item(article)
        if item is not None:
            items.append(item)
    return items
