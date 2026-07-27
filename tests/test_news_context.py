"""Unit tests for gather_news_context (both feed clients mocked)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.scanner import news_context
from app.integrations.alpha_vantage import AlphaVantageClient
from app.schemas.news_context import NewsContext


def _av_feed_item(
    url: str, title: str = "Headline", *, minutes_ago: int = 0, sentiment: float = 0.2
) -> dict:
    published = datetime(2026, 7, 27, 12, 0, 0) - timedelta(minutes=minutes_ago)
    return {
        "url": url,
        "title": title,
        "source_domain": "reuters.com",
        "overall_sentiment_score": sentiment,
        "time_published": published.strftime("%Y%m%dT%H%M%S"),
    }


def _gdelt_raw_item(url: str, title: str = "GDELT Headline") -> dict:
    return {
        "title": title,
        "domain": "example.com",
        "url": url,
        "sentiment": None,
        "seen_date": datetime(2026, 7, 27, 11, 0, 0, tzinfo=timezone.utc),
        "source_feed": "gdelt",
    }


def _enabled_av_client(
    monkeypatch: pytest.MonkeyPatch, feed: list[dict]
) -> AlphaVantageClient:
    client = AlphaVantageClient(api_key="test")
    monkeypatch.setattr(client, "get_news_sentiment", lambda **_kw: feed)
    return client


def test_gather_news_context_dedupes_by_url(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_url = "https://example.com/shared"
    av_client = _enabled_av_client(
        monkeypatch,
        [_av_feed_item(shared_url), _av_feed_item("https://example.com/av-only")],
    )
    monkeypatch.setattr(
        news_context.gdelt,
        "fetch_news_items",
        lambda _sectors: [
            _gdelt_raw_item(shared_url),
            _gdelt_raw_item("https://example.com/gdelt-only"),
        ],
    )

    result = news_context.gather_news_context(["Technology"], av_client)

    urls = [item.url for item in result.items]
    assert urls.count(shared_url) == 1
    assert "https://example.com/av-only" in urls
    assert "https://example.com/gdelt-only" in urls
    assert len(result.items) == 3


def test_gather_news_context_caps_at_15_items(monkeypatch: pytest.MonkeyPatch) -> None:
    av_feed = [
        _av_feed_item(f"https://example.com/{i}", minutes_ago=i) for i in range(20)
    ]
    av_client = _enabled_av_client(monkeypatch, av_feed)
    monkeypatch.setattr(news_context.gdelt, "fetch_news_items", lambda _sectors: [])

    result = news_context.gather_news_context(["Technology"], av_client)

    assert len(result.items) == 15
    assert result.degraded is False


def test_gather_news_context_degraded_when_both_feeds_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    av_client = _enabled_av_client(monkeypatch, [])
    monkeypatch.setattr(news_context.gdelt, "fetch_news_items", lambda _sectors: [])

    result = news_context.gather_news_context(["Technology"], av_client)

    assert result.items == []
    assert result.degraded is True
    assert isinstance(result, NewsContext)


def test_gather_news_context_not_degraded_when_only_gdelt_has_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    av_client = _enabled_av_client(monkeypatch, [])
    monkeypatch.setattr(
        news_context.gdelt,
        "fetch_news_items",
        lambda _sectors: [_gdelt_raw_item("https://example.com/x")],
    )

    result = news_context.gather_news_context(["Energy"], av_client)

    assert result.degraded is False
    assert len(result.items) == 1


def test_gather_news_context_none_av_client_skips_alpha_vantage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        news_context.gdelt,
        "fetch_news_items",
        lambda _sectors: [_gdelt_raw_item("https://example.com/x")],
    )

    result = news_context.gather_news_context(["Energy"], None)

    assert len(result.items) == 1
    assert result.items[0].source_feed == "gdelt"


def test_gather_news_context_disabled_av_client_skips_alpha_vantage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    av_client = AlphaVantageClient(api_key=None)  # disabled: no api key
    monkeypatch.setattr(news_context.gdelt, "fetch_news_items", lambda _sectors: [])

    result = news_context.gather_news_context(["Energy"], av_client)

    assert result.items == []
    assert result.degraded is True


def test_gather_news_context_sets_sector_and_macro_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    av_client = _enabled_av_client(monkeypatch, [])
    monkeypatch.setattr(news_context.gdelt, "fetch_news_items", lambda _sectors: [])

    result = news_context.gather_news_context(["Technology", "Energy"], av_client)

    assert result.sector_queries == ["Technology", "Energy"]
    assert result.macro_query == news_context.gdelt.MACRO_QUERY
    assert isinstance(result.fetched_at, datetime)
