"""Unit tests for the market-breadth client (requests mocked, no network)."""

from __future__ import annotations

import requests
from tenacity import nap
import pytest

import app.integrations.market_breadth as mb

_HEADER = (
    "Date,S&P500_Price,Breadth_Index_Raw,Breadth_Index_200MA,"
    "Breadth_Index_8MA,Breadth_200MA_Trend,Bearish_Signal,Is_Peak,"
    "Is_Trough,Is_Trough_8MA_Below_04\n"
)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _csv(rows: str) -> str:
    return _HEADER + rows


def test_parses_latest_row_by_date(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _csv(
        "2026-07-20,5000,0.55,0.5,0.52,1,False,False,False,False\n"
        "2026-07-27,5100,0.6234,0.5,0.58,-1,True,False,False,False\n"
    )
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(text))

    breadth = mb.fetch_market_breadth()

    assert breadth is not None
    assert breadth.pct_above_200dma == 62.34  # latest row, 0.6234 -> 62.34%
    assert breadth.smoothed_8ma == 58.0
    assert breadth.trend_rising is False
    assert breadth.bearish_signal is True
    assert breadth.as_of == "2026-07-27"
    assert breadth.days_old is not None  # computed against today


def test_empty_feed_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(_csv("")))
    assert mb.fetch_market_breadth() is None


def test_network_failure_exhausts_retries_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Neutralise tenacity's exponential backoff so the test doesn't sleep.
    monkeypatch.setattr(nap.time, "sleep", lambda _s: None)

    def _boom(*_a: object, **_k: object) -> _FakeResponse:
        raise requests.RequestException("down")

    monkeypatch.setattr(requests, "get", _boom)
    assert mb.fetch_market_breadth() is None


def test_malformed_row_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _csv("2026-07-27,5100,not-a-number,0.5,0.58,-1,True,False,False,False\n")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(text))
    assert mb.fetch_market_breadth() is None
