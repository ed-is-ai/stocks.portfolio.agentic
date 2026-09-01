"""Unit tests for the market-breadth client (requests mocked, no network)."""

from __future__ import annotations

from datetime import date

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


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    mb.reset_market_breadth_cache()
    yield
    mb.reset_market_breadth_cache()


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
    assert breadth.retrieval_source == "fetched"


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


def test_same_utc_day_reuses_cached_copy_without_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    text = _csv("2026-08-27,5100,0.6,0.5,0.58,1,False,False,False,False\n")

    def _get(*_args: object, **_kwargs: object) -> _FakeResponse:
        nonlocal calls
        calls += 1
        return _FakeResponse(text)

    monkeypatch.setattr(mb, "_utc_today", lambda: date(2026, 8, 27))
    monkeypatch.setattr(requests, "get", _get)

    fetched = mb.fetch_market_breadth()
    cached = mb.fetch_market_breadth()

    assert fetched is not None and cached is not None
    assert calls == 1
    assert fetched.retrieval_source == "fetched"
    assert cached.retrieval_source == "cached"
    cached.pct_above_200dma = 1.0
    again = mb.fetch_market_breadth()
    assert again is not None
    assert again.pct_above_200dma == 60.0


def test_utc_rollover_fetches_a_new_result(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    today = date(2026, 8, 27)
    text = _csv("2026-08-27,5100,0.6,0.5,0.58,1,False,False,False,False\n")

    def _get(*_args: object, **_kwargs: object) -> _FakeResponse:
        nonlocal calls
        calls += 1
        return _FakeResponse(text)

    monkeypatch.setattr(mb, "_utc_today", lambda: today)
    monkeypatch.setattr(requests, "get", _get)

    assert mb.fetch_market_breadth() is not None
    today = date(2026, 8, 28)
    rolled = mb.fetch_market_breadth()

    assert rolled is not None
    assert calls == 2
    assert rolled.retrieval_source == "fetched"


def test_failed_rollover_does_not_overwrite_valid_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = date(2026, 8, 27)
    valid = _csv("2026-08-27,5100,0.6,0.5,0.58,1,False,False,False,False\n")
    monkeypatch.setattr(mb, "_utc_today", lambda: today)
    monkeypatch.setattr(requests, "get", lambda *_a, **_k: _FakeResponse(valid))
    assert mb.fetch_market_breadth() is not None

    today = date(2026, 8, 28)
    monkeypatch.setattr(requests, "get", lambda *_a, **_k: _FakeResponse(_csv("")))
    assert mb.fetch_market_breadth() is None

    cached_date, cached = mb._daily_cache[mb._BREADTH_CSV_URL]
    assert cached_date == date(2026, 8, 27)
    assert cached.pct_above_200dma == 60.0


def test_stale_source_data_retains_staleness_when_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mb, "_utc_today", lambda: date(2026, 8, 27))
    text = _csv("2026-08-01,5100,0.6,0.5,0.58,1,False,False,False,False\n")
    monkeypatch.setattr(requests, "get", lambda *_a, **_k: _FakeResponse(text))

    fetched = mb.fetch_market_breadth()
    cached = mb.fetch_market_breadth()

    assert fetched is not None and cached is not None
    assert fetched.is_fresh is False and cached.is_fresh is False
    assert fetched.days_old == cached.days_old == 26
    assert fetched.retrieval_source == "fetched"
    assert cached.retrieval_source == "cached"


_HEADER_17 = (
    "Date,S&P500_Price,Breadth_Index_Raw,Breadth_Index_200MA,Breadth_Index_8MA,"
    "Breadth_200MA_Trend,Bearish_Signal,Is_Peak,Is_Trough,Is_Trough_8MA_Below_04,"
    "Breadth_50_Index_Raw,Breadth_50_Index_50MA,Breadth_50_Index_8MA,"
    "Breadth_50_MA_Trend,Bearish_Signal_50,Is_Peak_50,Is_Trough_50\n"
)


def _row_17(date_str: str, raw: str, raw_50: str = "0.50", trend: str = "1") -> str:
    return (
        f"{date_str},5000,{raw},0.5,0.6,{trend},False,False,False,False,"
        f"{raw_50},0.5,0.5,1,False,False,False\n"
    )


_FALLING_DAYS = [
    ("2026-08-20", "0.74"),
    ("2026-08-22", "0.72"),
    ("2026-08-24", "0.71"),
    ("2026-08-25", "0.70"),
    ("2026-08-27", "0.69"),
    ("2026-08-31", "0.68"),
]


def test_falling_week_keeps_positive_long_term_trend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _HEADER_17 + "".join(_row_17(d, raw) for d, raw in _FALLING_DAYS)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(text))

    breadth = mb.fetch_market_breadth()

    assert breadth is not None
    assert breadth.pct_above_200dma == 68.0
    assert breadth.trend_rising is True
    assert breadth.near_term_pct_delta == pytest.approx(-6.0)
    assert breadth.pct_50dma == 50.0
    assert breadth.near_term_50dma_pct_delta == pytest.approx(0.0)


def test_short_feed_leaves_near_term_fields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _HEADER_17 + "".join(_row_17(d, raw) for d, raw in _FALLING_DAYS[-5:])
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(text))

    breadth = mb.fetch_market_breadth()

    assert breadth is not None
    assert breadth.near_term_pct_delta is None
    assert breadth.near_term_50dma_pct_delta is None
    assert breadth.pct_50dma == 50.0  # latest-row read still works


def test_missing_50day_columns_leave_50day_fields_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = "".join(
        f"{d},5000,{raw},0.5,0.6,1,False,False,False,False\n"
        for d, raw in _FALLING_DAYS
    )
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(_csv(rows)))

    breadth = mb.fetch_market_breadth()

    assert breadth is not None
    assert breadth.pct_50dma is None
    assert breadth.near_term_50dma_pct_delta is None
    assert breadth.near_term_bearish_signal is False
    assert breadth.near_term_pct_delta == pytest.approx(-6.0)


def test_wide_prior_gap_nulls_near_term_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = [("2026-07-01", "0.74")] + _FALLING_DAYS[1:]
    text = _HEADER_17 + "".join(_row_17(d, raw) for d, raw in days)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(text))

    breadth = mb.fetch_market_breadth()

    assert breadth is not None
    assert breadth.pct_above_200dma == 68.0
    assert breadth.near_term_pct_delta is None  # prior row ~60 days back
    assert breadth.pct_50dma == 50.0  # latest-row read still works


def test_nan_prior_breadth_nulls_delta_not_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = [("2026-08-20", "nan")] + _FALLING_DAYS[1:]
    text = _HEADER_17 + "".join(_row_17(d, raw) for d, raw in days)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(text))

    breadth = mb.fetch_market_breadth()

    assert breadth is not None
    assert breadth.pct_above_200dma == 68.0
    assert breadth.near_term_pct_delta is None  # not NaN
    assert breadth.pct_50dma == 50.0


def test_row_with_none_date_does_not_crash_sort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = (
        "Breadth_Index_Raw,S&P500_Price,Breadth_Index_200MA,Breadth_Index_8MA,"
        "Breadth_200MA_Trend,Bearish_Signal,Is_Peak,Is_Trough,"
        "Is_Trough_8MA_Below_04,Date\n"
    )
    text = header + "0.72\n0.68,5000,0.5,0.6,1,False,False,False,False,2026-08-27\n"
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(text))

    breadth = mb.fetch_market_breadth()

    assert breadth is not None
    assert breadth.pct_above_200dma == 68.0


def test_unparseable_prior_row_nulls_only_near_term_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = [("2026-08-20", "bad")] + _FALLING_DAYS[1:]
    text = _HEADER_17 + "".join(_row_17(d, raw) for d, raw in days)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(text))

    breadth = mb.fetch_market_breadth()

    assert breadth is not None
    assert breadth.pct_above_200dma == 68.0
    assert breadth.near_term_pct_delta is None
    assert breadth.pct_50dma == 50.0
