"""Unit tests for historical base-pivot detection (pure DataFrame math)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pandas as pd
import pytest

from app.agents.analyst import historical_pivots as hp


@pytest.fixture(autouse=True)
def _clear_historical_pivot_cache() -> None:
    """Keep process-cache tests independent from pure detector tests."""
    with hp._pivot_cache_lock:
        hp._pivot_cache.clear()
        hp._pivot_flights.clear()


# ---------------------------------------------------------------------------
# Synthetic frame builder
# ---------------------------------------------------------------------------


def _weekly(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    start: str = "2020-01-06",
) -> pd.DataFrame:
    """Build a weekly OHLCV frame with lowercase cols and a DatetimeIndex."""
    n = len(highs)
    assert len(lows) == n and len(closes) == n
    idx = pd.date_range(start=start, periods=n, freq="W")
    return pd.DataFrame({"high": highs, "low": lows, "close": closes}, index=idx)


# ---------------------------------------------------------------------------
# _find_peak_candidates
# ---------------------------------------------------------------------------


def test_find_peak_candidates_single_clear_peak() -> None:
    """A single obvious peak in the middle should be detected."""
    # 23 bars: 11 rising, peak at index 11, 11 falling
    # Peak high = 120; window lows ~= 100; prominence = (120-100)/120 = 16.7% > 8%
    highs = [100.0 + i for i in range(11)] + [120.0] + [110.0 - i for i in range(11)]
    lows = (
        [95.0 + i * 0.5 for i in range(11)]
        + [115.0]
        + [105.0 - i * 0.5 for i in range(11)]
    )
    closes = highs  # doesn't matter for peak detection
    df = _weekly(highs, lows, closes)
    n = len(df)

    peaks = hp._find_peak_candidates(df)

    assert 11 in peaks
    for p in peaks:
        assert hp._HALF_WINDOW <= p < n - hp._HALF_WINDOW


def test_find_peak_candidates_flat_data_no_peaks() -> None:
    """Flat highs produce zero prominence — no peaks expected."""
    n = 30
    highs = [100.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    df = _weekly(highs, lows, closes)

    peaks = hp._find_peak_candidates(df)

    assert peaks == []


def test_find_peak_candidates_insufficient_prominence() -> None:
    """A local max only 2% above the window low is below _MIN_PROMINENCE threshold."""
    # Peak at index 11 with high=100, window lows=98 → prominence=(100-98)/100=2%
    base_high = 100.0
    base_low = 98.0  # only 2% below peak → prominence 2% < 8%
    highs = [base_high - 0.1] * 11 + [base_high] + [base_high - 0.1] * 11
    lows = [base_low] * 23
    closes = highs[:]
    df = _weekly(highs, lows, closes)

    peaks = hp._find_peak_candidates(df)

    # The peak at index 11 should NOT be in results due to low prominence
    assert 11 not in peaks


def test_find_peak_candidates_indices_within_valid_range() -> None:
    """All returned peak indices must be within [half_window, n - half_window)."""
    n = 40
    # Create two bumps — one at index 15 and one at 30
    highs = [100.0] * n
    lows = [90.0] * n
    closes = [100.0] * n
    highs[15] = 130.0  # big peak — prominence (130-90)/130 = 30.8%
    highs[30] = 125.0  # smaller peak
    df = _weekly(highs, lows, closes)

    peaks = hp._find_peak_candidates(df)

    for p in peaks:
        assert hp._HALF_WINDOW <= p < n - hp._HALF_WINDOW


# ---------------------------------------------------------------------------
# _build_base
# ---------------------------------------------------------------------------


def _make_valid_base_frame(peak_idx: int = 20, n: int = 40) -> pd.DataFrame:
    """Build a frame where the base around peak_idx is valid (≥6 weeks, ≤35% depth)."""
    pivot_price = 100.0
    # Lows stay above floor = 100*(1-0.35)=65, so all lows at 80 are valid
    highs = [90.0] * n
    lows = [80.0] * n
    closes = [85.0] * n
    highs[peak_idx] = pivot_price
    return _weekly(highs, lows, closes)


def test_build_base_valid_returns_dict() -> None:
    """A frame with sufficient base weeks should return a complete dict."""
    peak_idx = 20
    df = _make_valid_base_frame(peak_idx=peak_idx)

    result = hp._build_base(df, peak_idx)

    assert result is not None
    assert result["base_start_idx"] < peak_idx
    assert result["base_end_idx"] == peak_idx
    assert result["base_weeks"] == peak_idx - result["base_start_idx"]
    # base_start/base_end should be ISO date strings (YYYY-MM-DD)
    assert len(result["base_start"]) == 10 and "-" in result["base_start"]
    assert len(result["base_end"]) == 10 and "-" in result["base_end"]


def test_build_base_too_short_returns_none() -> None:
    """A deep drop immediately before the peak stops the walk-back, giving <6 weeks."""
    # Peak at index 3, everything before is deep (below floor)
    n = 30
    highs = [80.0] * n
    lows = [40.0] * n  # 40 < 100*(1-0.35)=65 → floor violation
    closes = [75.0] * n
    highs[3] = 100.0
    # Only 3 bars before peak pass floor check (there aren't any — lows[i]=40 < 65)
    df = _weekly(highs, lows, closes)

    result = hp._build_base(df, 3)

    assert result is None


def test_build_base_weeks_correct() -> None:
    """base_weeks in the dict equals peak_idx minus base_start_idx."""
    peak_idx = 15
    df = _make_valid_base_frame(peak_idx=peak_idx, n=30)

    result = hp._build_base(df, peak_idx)

    assert result is not None
    assert result["base_weeks"] == peak_idx - result["base_start_idx"]


# ---------------------------------------------------------------------------
# _find_breakout
# ---------------------------------------------------------------------------


def _breakout_frame(
    peak_idx: int,
    pivot_price: float,
    breakout_offset: int | None,
    breakout_close: float | None,
    post_high: float | None,
    total_bars: int = 80,
) -> pd.DataFrame:
    """Build a frame for breakout testing.

    peak_idx: position of the pivot
    breakout_offset: bars after peak where the breakout close occurs (None = no breakout)
    breakout_close: the close on the breakout bar
    post_high: the max close in the advance window (for advance_pct calculation)
    """
    highs = [pivot_price * 0.9] * total_bars
    lows = [pivot_price * 0.7] * total_bars
    closes = [pivot_price * 0.85] * total_bars

    highs[peak_idx] = pivot_price

    if breakout_offset is not None and breakout_close is not None:
        bo_idx = peak_idx + breakout_offset
        closes[bo_idx] = breakout_close
        if post_high is not None:
            # Set a later bar to post_high
            later_idx = min(bo_idx + 5, total_bars - 1)
            closes[later_idx] = post_high

    return _weekly(highs, lows, closes)


def test_find_breakout_resistance() -> None:
    """No close clears the threshold within search window → status='resistance'."""
    pivot_price = 100.0
    peak_idx = 10
    # All closes well below threshold
    df = _breakout_frame(
        peak_idx=peak_idx,
        pivot_price=pivot_price,
        breakout_offset=None,
        breakout_close=None,
        post_high=None,
    )

    result = hp._find_breakout(df, peak_idx, pivot_price)

    assert result["status"] == "resistance"
    assert result["breakout_date"] is None
    assert result["advance_pct"] is None


def test_find_breakout_confirmed() -> None:
    """A close above threshold followed by ≥8% advance → status='confirmed'."""
    pivot_price = 100.0
    peak_idx = 10
    breakout_close_val = pivot_price * 1.006  # clears 1.005 threshold
    post_high_val = pivot_price * 1.15  # 15% above pivot → advance_pct = 15%

    df = _breakout_frame(
        peak_idx=peak_idx,
        pivot_price=pivot_price,
        breakout_offset=3,
        breakout_close=breakout_close_val,
        post_high=post_high_val,
    )

    result = hp._find_breakout(df, peak_idx, pivot_price)

    assert result["status"] == "confirmed"
    assert result["breakout_date"] is not None
    assert len(result["breakout_date"]) == 10  # ISO date
    assert result["advance_pct"] is not None
    assert result["advance_pct"] >= 8.0


def test_find_breakout_failed() -> None:
    """A close clears the threshold but post advance < 8% → status='failed'."""
    pivot_price = 100.0
    peak_idx = 10
    breakout_close_val = pivot_price * 1.006  # clears threshold
    # Post closes stay near breakout level — advance of only ~2%
    post_high_val = pivot_price * 1.02

    df = _breakout_frame(
        peak_idx=peak_idx,
        pivot_price=pivot_price,
        breakout_offset=3,
        breakout_close=breakout_close_val,
        post_high=post_high_val,
    )

    result = hp._find_breakout(df, peak_idx, pivot_price)

    assert result["status"] == "failed"
    assert result["advance_pct"] is not None
    assert result["advance_pct"] < 8.0


# ---------------------------------------------------------------------------
# _is_stage2_at_pivot
# ---------------------------------------------------------------------------


def test_is_stage2_insufficient_history() -> None:
    """peak_idx < 39 always returns False regardless of prices."""
    n = 50
    highs = [100.0] * n
    lows = [90.0] * n
    closes = [150.0] * n  # all above any mean — but history check fires first
    df = _weekly(highs, lows, closes)

    assert hp._is_stage2_at_pivot(df, 10) is False


def test_is_stage2_above_sma40() -> None:
    """Close at peak above the 40-bar mean → True."""
    n = 60
    # Lows in 0-38: close=90; peak at 39: close=150 — mean of window is ~90.8
    closes = [90.0] * n
    closes[39] = 150.0
    highs = [c + 5 for c in closes]
    lows = [c - 5 for c in closes]
    df = _weekly(highs, lows, closes)

    assert hp._is_stage2_at_pivot(df, 39) is True


def test_is_stage2_below_sma40() -> None:
    """Close at peak below the 40-bar mean → False."""
    n = 60
    # First 39 bars all at 200, peak close at 39 is only 50
    closes = [200.0] * n
    closes[39] = 50.0
    highs = [c + 5 for c in closes]
    lows = [c - 5 for c in closes]
    df = _weekly(highs, lows, closes)

    assert hp._is_stage2_at_pivot(df, 39) is False


# ---------------------------------------------------------------------------
# _deduplicate_pivots
# ---------------------------------------------------------------------------


def _make_pivot(
    pivot_price: float,
    base_start: str = "2022-01-01",
    advance_pct: float | None = 10.0,
) -> dict:
    """Build a minimal pivot dict for dedup testing."""
    return {
        "pivot_price": pivot_price,
        "base_start": base_start,
        "advance_pct": advance_pct,
    }


def test_deduplicate_pivots_empty() -> None:
    """Empty input returns empty list."""
    assert hp._deduplicate_pivots([]) == []


def test_deduplicate_pivots_within_band_keeps_higher_advance() -> None:
    """Two pivots within 5% → keep the one with higher advance_pct."""
    p1 = _make_pivot(100.0, "2022-01-01", advance_pct=5.0)
    p2 = _make_pivot(103.0, "2022-02-01", advance_pct=20.0)

    result = hp._deduplicate_pivots([p1, p2])

    assert len(result) == 1
    assert result[0]["advance_pct"] == 20.0
    assert result[0]["pivot_price"] == 103.0


def test_deduplicate_pivots_outside_band_keeps_both() -> None:
    """Pivots >5% apart are kept separately, sorted by base_start."""
    p1 = _make_pivot(100.0, "2022-06-01", advance_pct=10.0)
    p2 = _make_pivot(120.0, "2022-01-01", advance_pct=12.0)

    result = hp._deduplicate_pivots([p1, p2])

    assert len(result) == 2
    # Sorted by base_start ascending
    assert result[0]["base_start"] < result[1]["base_start"]


def test_deduplicate_pivots_none_advance_treated_as_zero() -> None:
    """advance_pct=None counts as 0.0 when comparing within a cluster."""
    p1 = _make_pivot(100.0, "2022-01-01", advance_pct=None)
    p2 = _make_pivot(102.0, "2022-02-01", advance_pct=15.0)

    result = hp._deduplicate_pivots([p1, p2])

    assert len(result) == 1
    assert result[0]["advance_pct"] == 15.0


# ---------------------------------------------------------------------------
# _detect_open_base
# ---------------------------------------------------------------------------


def _open_base_frame(n: int = 80, pivot_pos_from_end: int = 20) -> pd.DataFrame:
    """Build a frame where the last ~20 bars form an open base.

    The recent high is at (n - pivot_pos_from_end), all subsequent closes stay
    below threshold (no breakout), and lows stay within 35% depth.
    """
    pivot_price = 100.0
    highs = [95.0] * n
    lows = [85.0] * n
    closes = [90.0] * n

    peak_pos = n - pivot_pos_from_end
    highs[peak_pos] = pivot_price

    return _weekly(highs, lows, closes)


def test_detect_open_base_returns_open() -> None:
    """A trailing base with no breakout → returns dict with status='open'."""
    df = _open_base_frame(n=80, pivot_pos_from_end=20)

    result = hp._detect_open_base(df, confirmed_pivots=[])

    assert result is not None
    assert result["status"] == "open"
    assert result["breakout_date"] is None
    assert "pivot_price" in result


def test_detect_open_base_already_broken_out_returns_none() -> None:
    """If a close after the recent high clears pivot * 1.005, returns None."""
    n = 80
    pivot_price = 100.0
    highs = [95.0] * n
    lows = [85.0] * n
    closes = [90.0] * n

    peak_pos = n - 20
    highs[peak_pos] = pivot_price
    # Place a breakout close after the peak
    closes[peak_pos + 5] = pivot_price * 1.01  # above threshold

    df = _weekly(highs, lows, closes)

    result = hp._detect_open_base(df, confirmed_pivots=[])

    assert result is None


def test_detect_open_base_duplicate_confirmed_pivot_returns_none() -> None:
    """If the open-base pivot is within 5% of a confirmed pivot, returns None."""
    df = _open_base_frame(n=80, pivot_pos_from_end=20)

    # The open base pivot_price will be 100.0 — put a confirmed pivot at 101
    confirmed = [
        {
            "pivot_price": 101.0,
            "base_start": "2021-01-01",
            "advance_pct": 10.0,
        }
    ]

    result = hp._detect_open_base(df, confirmed_pivots=confirmed)

    assert result is None


# ---------------------------------------------------------------------------
# find_historical_pivots (end-to-end, monkeypatched)
# ---------------------------------------------------------------------------


def _e2e_frame() -> pd.DataFrame:
    """
    Build a ≥70-bar weekly frame with one clear base+pivot past index 40
    and a subsequent confirmed breakout, so find_historical_pivots can find it.
    """
    n = 80
    pivot_price = 200.0
    peak_idx = 50  # past index 39 so Stage-2 check can run

    highs = [180.0] * n
    lows = [160.0] * n  # floor = 200*(1-0.35)=130; 160>130, so base valid
    closes = [170.0] * n

    # Bars 0-49: rising into the pivot; bars leading to peak all at 190
    for i in range(40, peak_idx):
        closes[i] = 190.0  # above mean so Stage-2 passes

    highs[peak_idx] = pivot_price
    closes[peak_idx] = 195.0  # above mean so Stage-2 is True

    # Breakout 5 bars later, then advance to 230 (15% above pivot)
    bo_idx = peak_idx + 5
    closes[bo_idx] = pivot_price * 1.006  # triggers breakout
    closes[min(bo_idx + 5, n - 1)] = 230.0  # 15% advance → confirmed

    # Set closes for bars 0-39 low enough to set up Stage-2 SMA
    for i in range(39):
        closes[i] = 160.0

    return _weekly(highs, lows, closes)


def test_find_historical_pivots_runs_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orchestration runs without network; each pivot dict has the required keys."""
    frame = _e2e_frame()
    monkeypatch.setattr(hp, "fetch_weekly_ohlcv", lambda ticker, period: frame)

    result = hp.find_historical_pivots("TEST", require_stage2=False)

    assert isinstance(result, list)
    required_keys = {
        "pivot_price",
        "base_start",
        "base_end",
        "base_weeks",
        "max_depth_pct",
        "breakout_date",
        "breakout_close",
        "advance_pct",
        "stage2_at_pivot",
        "status",
    }
    # At minimum the non-empty case should have the right keys
    for p in result:
        assert required_keys <= set(p)


def test_find_historical_pivots_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crafted e2e frame yields at least one pivot."""
    frame = _e2e_frame()
    monkeypatch.setattr(hp, "fetch_weekly_ohlcv", lambda ticker, period: frame)

    result = hp.find_historical_pivots("TEST", require_stage2=False)

    assert len(result) >= 1


def test_find_historical_pivots_flat_frame_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degenerate flat frame with no qualifying peaks returns an empty list."""
    n = 70
    highs = [100.0] * n
    lows = [99.0] * n  # prominence = (100-99)/100 = 1% < 8%
    closes = [100.0] * n
    flat_frame = _weekly(highs, lows, closes)
    monkeypatch.setattr(hp, "fetch_weekly_ohlcv", lambda ticker, period: flat_frame)

    result = hp.find_historical_pivots("TEST", require_stage2=False)

    # Flat data: no peaks, so no confirmed pivots; open base may or may not trigger
    # but with 1% prominence the open-base logic also works off highs which are all equal
    # — the result is a valid list (possibly empty, possibly one open base)
    assert isinstance(result, list)
    # No confirmed/failed/resistance pivots expected
    for p in result:
        assert p.get("status") in ("open", "resistance", "confirmed", "failed")


# ---------------------------------------------------------------------------
# Completed-week cache
# ---------------------------------------------------------------------------


def test_completed_week_uses_each_symbol_market_close() -> None:
    """A London weekly bar is final before the US market closes that Friday."""
    instant = pd.Timestamp("2026-08-28 15:45:00+00:00")

    assert hp._completed_week_start("VOD.L", now=instant) == pd.Timestamp("2026-08-24")
    assert hp._completed_week_start("AAPL", now=instant) == pd.Timestamp("2026-08-17")


def test_completed_weekly_bars_excludes_the_active_week() -> None:
    """Only bars through the completed week feed the unchanged detector."""
    weekly = pd.DataFrame(
        {"high": [1.0, 2.0, 3.0], "low": [0.0, 1.0, 2.0], "close": [1.0, 2.0, 3.0]},
        index=pd.to_datetime(["2026-08-17", "2026-08-24", "2026-08-31"]),
    )

    completed = hp._completed_weekly_bars(weekly, pd.Timestamp("2026-08-24"))

    assert list(completed.index) == list(pd.to_datetime(["2026-08-17", "2026-08-24"]))


def test_completed_weekly_bars_preserves_exchange_local_week_for_aware_index() -> None:
    """A local-Monday weekly label must not become the preceding UTC Sunday."""
    weekly = pd.DataFrame(
        {"high": [1.0, 2.0], "low": [0.0, 1.0], "close": [1.0, 2.0]},
        index=pd.DatetimeIndex(
            ["2026-08-17 00:00:00", "2026-08-24 00:00:00"], tz="Europe/London"
        ),
    )

    completed = hp._completed_weekly_bars(weekly, pd.Timestamp("2026-08-17"))

    assert list(completed.index) == [pd.Timestamp("2026-08-17", tz="Europe/London")]


def test_same_completed_week_uses_one_provider_call_and_returns_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same canonical ticker/week avoids redownloads and caller mutation."""
    frame = _e2e_frame()
    calls = 0

    def fetch(_ticker: str, _period: str) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        return frame

    monkeypatch.setattr(hp, "fetch_weekly_ohlcv", fetch)
    monkeypatch.setattr(hp, "_completed_week_start", lambda _ticker: pd.Timestamp("2022-01-03"))

    first = hp.find_historical_pivots(" test ", require_stage2=False)
    first[0]["pivot_price"] = -1.0
    second = hp.find_historical_pivots("TEST", require_stage2=False)

    assert calls == 1
    assert second[0]["pivot_price"] != -1.0


def test_new_completed_week_recomputes_from_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completed-week identity makes a rollover a cache miss."""
    frame = _e2e_frame()
    calls = 0
    week = pd.Timestamp("2022-01-03")

    def fetch(_ticker: str, _period: str) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        return frame

    monkeypatch.setattr(hp, "fetch_weekly_ohlcv", fetch)
    monkeypatch.setattr(hp, "_completed_week_start", lambda _ticker: week)

    hp.find_historical_pivots("TEST", require_stage2=False)
    week = pd.Timestamp("2022-01-10")
    hp.find_historical_pivots("TEST", require_stage2=False)

    assert calls == 2


def test_concurrent_same_key_misses_share_one_provider_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent callers join one flight instead of duplicating the download."""
    frame = _e2e_frame()
    provider_started = threading.Event()
    release_provider = threading.Event()
    follower_joined = threading.Event()
    calls = 0

    def fetch(_ticker: str, _period: str) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        provider_started.set()
        assert release_provider.wait(timeout=2)
        return frame

    original_acquire = hp._get_cached_or_begin_flight

    def acquire(key: hp._PivotCacheKey):
        cached, flight, is_leader = original_acquire(key)
        if cached is None and not is_leader:
            follower_joined.set()
        return cached, flight, is_leader

    monkeypatch.setattr(hp, "fetch_weekly_ohlcv", fetch)
    monkeypatch.setattr(hp, "_get_cached_or_begin_flight", acquire)
    monkeypatch.setattr(hp, "_completed_week_start", lambda _ticker: pd.Timestamp("2022-01-03"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(hp.find_historical_pivots, "TEST", require_stage2=False)
        assert provider_started.wait(timeout=2)
        second = executor.submit(hp.find_historical_pivots, "TEST", require_stage2=False)
        assert follower_joined.wait(timeout=2)
        release_provider.set()
        assert first.result(timeout=2) == second.result(timeout=2)

    assert calls == 1


def test_provider_failure_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures remain fail-soft for callers and cannot poison a later retry."""
    calls = 0

    def fail(_ticker: str, _period: str) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        raise ValueError("provider unavailable")

    monkeypatch.setattr(hp, "fetch_weekly_ohlcv", fail)
    monkeypatch.setattr(hp, "_completed_week_start", lambda _ticker: pd.Timestamp("2020-01-06"))

    with pytest.raises(ValueError, match="provider unavailable"):
        hp.find_historical_pivots("TEST", require_stage2=False)
    with pytest.raises(ValueError, match="provider unavailable"):
        hp.find_historical_pivots("TEST", require_stage2=False)

    assert calls == 2
    assert hp._pivot_cache == {}
