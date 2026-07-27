"""Unit tests for the GDELT DOC 2.0 client (requests mocked, no network)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import requests

from app.integrations import gdelt


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def test_build_sector_query_empty_returns_empty_string() -> None:
    assert gdelt.build_sector_query([]) == ""


def test_build_sector_query_joins_sectors() -> None:
    query = gdelt.build_sector_query(["Technology", "Energy"])
    assert '"Technology"' in query
    assert '"Energy"' in query
    assert "OR" in query
    assert "sourcelang:english" in query


def test_fetch_news_items_success_parses_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "articles": [
            {
                "url": "https://example.com/a",
                "title": "Fed holds rates steady",
                "domain": "example.com",
                "seendate": "20260726T120000Z",
            }
        ]
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload))
    items = gdelt.fetch_news_items(["Technology"])
    assert len(items) == 2  # sector query + macro query, same payload each
    item = items[0]
    assert item["title"] == "Fed holds rates steady"
    assert item["domain"] == "example.com"
    assert item["url"] == "https://example.com/a"
    assert item["sentiment"] is None
    assert item["source_feed"] == "gdelt"
    assert item["seen_date"] == datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def test_fetch_news_items_no_sectors_skips_sector_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_get(*_args: Any, **kwargs: Any) -> _FakeResponse:
        calls.append(kwargs.get("params", {}))
        return _FakeResponse({"articles": []})

    monkeypatch.setattr(requests, "get", _fake_get)
    items = gdelt.fetch_news_items([])
    assert items == []
    assert len(calls) == 1  # only the macro query


def test_fetch_news_items_empty_articles_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse({"articles": []})
    )
    assert gdelt.fetch_news_items(["Energy"]) == []


def test_fetch_news_items_missing_url_or_title_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "articles": [
            {"url": "https://example.com/a"},  # missing title
            {"title": "No URL here"},  # missing url
        ]
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload))
    assert gdelt.fetch_news_items([]) == []


def test_fetch_news_items_retry_exhaustion_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every call raises → tenacity exhausts retries → [] not an exception."""

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", _raise)
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda *_a, **_k: None)
    assert gdelt.fetch_news_items(["Technology"]) == []
