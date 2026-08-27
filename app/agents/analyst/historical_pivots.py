"""Detect historical base pivot/breakout levels from multi-year weekly OHLCV data.

Identifies Minervini-style consolidation bases and the pivot price at the top of
each base, along with confirmation of the subsequent breakout and advance.

Usage:
    python app/agents/analyst/historical_pivots.py WELL
    python app/agents/analyst/historical_pivots.py WELL --period 7y --no-stage2
    python app/agents/analyst/historical_pivots.py WELL --json
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from copy import deepcopy
from functools import lru_cache
import json
import threading
from typing import Any

import exchange_calendars as xcals
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------

_DEFAULT_PERIOD = "10y"
_HALF_WINDOW = 10  # weeks each side to qualify a local high as a peak
_MIN_PROMINENCE = 0.08  # peak must be ≥8% above the lowest low in the window
_MIN_BASE_WEEKS = 6  # consolidation must last at least 6 weeks
_MAX_BASE_DEPTH = 35.0  # base cannot drop more than 35% from pivot (%)
_MIN_ADVANCE_PCT = 8.0  # advance after breakout must be ≥8% to confirm
_BREAKOUT_SEARCH_WEEKS = 26
_ADVANCE_WINDOW_WEEKS = 52
_DEDUP_BAND = 0.05  # merge pivots within 5% of each other
_BREAKOUT_THRESHOLD = 1.005  # close must be > pivot * this to count as breakout
_MIN_HISTORY_BARS = 60  # raise if fewer weekly bars than this

# Historical pivot analysis is invoked repeatedly by both VCP scoring and the
# multi-year-breakout check.  Weekly bars only become evidence once their
# exchange's final session has closed, so cache by that completed week rather
# than by wall-clock time.  This remains a deliberately small process cache:
# it is an optimisation, not durable strategy evidence.
_PIVOT_CACHE_MAX_ENTRIES = 256
_PivotCacheKey = tuple[str, str, int, int, float, float, bool, str]
_pivot_cache: OrderedDict[_PivotCacheKey, list[dict[str, Any]]] = OrderedDict()
_pivot_cache_lock = threading.Lock()


class _PivotFlight:
    """One in-progress calculation shared by concurrent callers for a cache key."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.result: list[dict[str, Any]] | None = None
        self.error: BaseException | None = None


_pivot_flights: dict[_PivotCacheKey, _PivotFlight] = {}


def _canonical_ticker(ticker: str) -> str:
    """Normalize the Yahoo symbol used as the in-process cache identity."""
    return ticker.strip().upper()


def _calendar_name_for_ticker(ticker: str) -> str:
    """Return the exchange calendar implied by this project's Yahoo symbols.

    The scanner and portfolio provider convention is ``.L`` for London and
    ``.HK`` for Hong Kong; unsuffixed symbols are its US equities convention.
    Keeping this mapping local avoids a second provider lookup solely to cache
    a result, while still respecting the non-US symbols the application uses.
    """
    symbol = _canonical_ticker(ticker)
    if symbol.endswith(".L"):
        return "XLON"
    if symbol.endswith(".HK"):
        return "XHKG"
    return "XNYS"


@lru_cache(maxsize=3)
def _exchange_calendar(name: str) -> Any:
    """Load one supported exchange calendar once per process."""
    return xcals.get_calendar(name)


def _completed_week_start(ticker: str, *, now: pd.Timestamp | None = None) -> pd.Timestamp:
    """Return Monday for the newest fully closed exchange trading week.

    A Friday is not considered complete until that exchange's actual final
    session close.  This captures holidays and early closes from
    ``exchange_calendars`` and makes the boundary injectable in tests.
    """
    calendar = _exchange_calendar(_calendar_name_for_ticker(ticker))
    instant = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if instant.tzinfo is None:
        instant = instant.tz_localize("UTC")
    local_now = instant.tz_convert(calendar.tz)
    today = pd.Timestamp(local_now.date())
    week_start = today - pd.Timedelta(days=today.weekday())
    week_end = week_start + pd.Timedelta(days=6)
    sessions = calendar.sessions_in_range(week_start, week_end)

    if len(sessions) and local_now >= calendar.session_close(sessions[-1]):
        return sessions[-1].to_period("W-SUN").start_time

    previous_week_start = week_start - pd.Timedelta(days=7)
    previous_sessions = calendar.sessions_in_range(
        previous_week_start, week_start - pd.Timedelta(days=1)
    )
    if not len(previous_sessions):  # pragma: no cover - calendars always have sessions
        raise ValueError(f"No completed trading week available for {ticker}")
    return previous_sessions[-1].to_period("W-SUN").start_time


def _completed_weekly_bars(
    weekly: pd.DataFrame, completed_week_start: pd.Timestamp
) -> pd.DataFrame:
    """Drop any weekly bar that belongs to a not-yet-completed trading week."""
    index = pd.DatetimeIndex(weekly.index)
    if index.tz is not None:
        # Weekly labels represent the exchange-local trading week. Converting
        # a midnight London/Hong Kong label to UTC can move it into Sunday.
        index = index.tz_localize(None)
    bar_week_starts = index.to_period("W-SUN").start_time
    return weekly.loc[bar_week_starts <= completed_week_start].copy()


def _cache_key(
    ticker: str,
    period: str,
    half_window: int,
    min_base_weeks: int,
    max_depth_pct: float,
    min_advance_pct: float,
    require_stage2: bool,
    completed_week_start: pd.Timestamp,
) -> _PivotCacheKey:
    """Include every detector input plus the completed-week identity."""
    return (
        _canonical_ticker(ticker),
        period,
        half_window,
        min_base_weeks,
        max_depth_pct,
        min_advance_pct,
        require_stage2,
        completed_week_start.date().isoformat(),
    )


def _get_cached_or_begin_flight(
    key: _PivotCacheKey,
) -> tuple[list[dict[str, Any]] | None, _PivotFlight | None, bool]:
    """Return a cached copy or join/create the key's single-flight calculation."""
    with _pivot_cache_lock:
        cached = _pivot_cache.get(key)
        if cached is not None:
            _pivot_cache.move_to_end(key)
            return deepcopy(cached), None, False

        flight = _pivot_flights.get(key)
        if flight is None:
            flight = _PivotFlight()
            _pivot_flights[key] = flight
            return None, flight, True
        return None, flight, False


def _finish_flight(
    key: _PivotCacheKey,
    flight: _PivotFlight,
    *,
    result: list[dict[str, Any]] | None = None,
    error: BaseException | None = None,
) -> None:
    """Publish a result or error and release every caller waiting on ``flight``."""
    with _pivot_cache_lock:
        if result is not None:
            flight.result = deepcopy(result)
            _pivot_cache[key] = deepcopy(result)
            _pivot_cache.move_to_end(key)
            while len(_pivot_cache) > _PIVOT_CACHE_MAX_ENTRIES:
                _pivot_cache.popitem(last=False)
        flight.error = error
        _pivot_flights.pop(key, None)
        flight.ready.set()


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------


def fetch_weekly_ohlcv(ticker: str, period: str = _DEFAULT_PERIOD) -> pd.DataFrame:
    """Return unadjusted weekly OHLCV with lowercase flat columns, index = date.

    Unadjusted prices are used so pivot levels match what appears on standard
    charts (important for dividend-paying stocks like REITs).
    """
    df: pd.DataFrame = yf.download(
        ticker,
        period=period,
        interval="1wk",
        progress=False,
        auto_adjust=False,
    )
    if df.empty:
        raise ValueError(f"yfinance returned no data for {ticker}")
    # Flatten MultiIndex columns emitted by newer yfinance versions
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    required = {"high", "low", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"Missing columns for {ticker}: {required - set(df.columns)}")
    df = df.dropna(subset=["high", "low", "close"])
    if len(df) < _MIN_HISTORY_BARS:
        raise ValueError(
            f"Only {len(df)} weekly bars for {ticker}; need ≥{_MIN_HISTORY_BARS}"
        )
    return df


# ---------------------------------------------------------------------------
# Peak detection
# ---------------------------------------------------------------------------


def _find_peak_candidates(
    weekly: pd.DataFrame, half_window: int = _HALF_WINDOW
) -> list[int]:
    """Return row indices where the weekly high is a local maximum with sufficient prominence."""
    highs = weekly["high"].to_numpy(dtype=float)
    lows = weekly["low"].to_numpy(dtype=float)
    n = len(highs)
    peaks: list[int] = []

    for i in range(half_window, n - half_window):
        window_highs = highs[i - half_window : i + half_window + 1]
        window_lows = lows[i - half_window : i + half_window + 1]
        if highs[i] < window_highs.max():
            continue
        # Prominence: peak must be meaningfully above the window's lowest low
        prominence = (highs[i] - window_lows.min()) / highs[i]
        if prominence < _MIN_PROMINENCE:
            continue
        # Avoid flat-top duplicates: only keep the first week of equal highs
        if i > 0 and highs[i] == highs[i - 1]:
            continue
        peaks.append(i)

    return peaks


# ---------------------------------------------------------------------------
# Base construction
# ---------------------------------------------------------------------------


def _build_base(
    weekly: pd.DataFrame,
    peak_idx: int,
    max_depth_pct: float = _MAX_BASE_DEPTH,
    min_base_weeks: int = _MIN_BASE_WEEKS,
) -> dict[str, Any] | None:
    """Walk back from peak_idx to find the consolidation base.

    Returns a dict with base_start, base_end, base_weeks, max_depth_pct,
    or None if the base is too short.
    """
    pivot_price = float(weekly["high"].iloc[peak_idx])
    lows = weekly["low"].to_numpy(dtype=float)
    floor = pivot_price * (1.0 - max_depth_pct / 100.0)

    base_start_idx = peak_idx
    for i in range(peak_idx - 1, max(0, peak_idx - 104), -1):
        if lows[i] < floor:
            break
        base_start_idx = i

    base_weeks = peak_idx - base_start_idx
    if base_weeks < min_base_weeks:
        return None

    base_lows = lows[base_start_idx : peak_idx + 1]
    depth = (pivot_price - float(base_lows.min())) / pivot_price * 100.0

    return {
        "base_start_idx": base_start_idx,
        "base_end_idx": peak_idx,
        "base_start": weekly.index[base_start_idx].date().isoformat(),
        "base_end": weekly.index[peak_idx].date().isoformat(),
        "base_weeks": base_weeks,
        "max_depth_pct": round(depth, 1),
    }


# ---------------------------------------------------------------------------
# Breakout search
# ---------------------------------------------------------------------------


def _find_breakout(
    weekly: pd.DataFrame,
    peak_idx: int,
    pivot_price: float,
    search_weeks: int = _BREAKOUT_SEARCH_WEEKS,
    min_advance_pct: float = _MIN_ADVANCE_PCT,
    advance_window: int = _ADVANCE_WINDOW_WEEKS,
) -> dict[str, Any]:
    """Search forward from peak_idx for a breakout close.

    Always returns a dict. advance_pct < min_advance_pct → status "failed".
    breakout_date/close are None when price never cleared the pivot.
    """
    closes = weekly["close"].to_numpy(dtype=float)
    n = len(closes)
    threshold = pivot_price * _BREAKOUT_THRESHOLD

    breakout_idx: int | None = None
    for i in range(peak_idx + 1, min(n, peak_idx + search_weeks + 1)):
        if closes[i] >= threshold:
            breakout_idx = i
            break

    if breakout_idx is None:
        return {
            "breakout_date": None,
            "breakout_close": None,
            "advance_pct": None,
            "status": "resistance",  # never broke out in the search window
        }

    look_ahead = min(n, breakout_idx + advance_window + 1)
    post_high = float(closes[breakout_idx:look_ahead].max())
    advance_pct = round((post_high - pivot_price) / pivot_price * 100.0, 1)

    status = "confirmed" if advance_pct >= min_advance_pct else "failed"
    return {
        "breakout_date": weekly.index[breakout_idx].date().isoformat(),
        "breakout_close": round(float(closes[breakout_idx]), 2),
        "advance_pct": advance_pct,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Stage 2 filter
# ---------------------------------------------------------------------------


def _is_stage2_at_pivot(weekly: pd.DataFrame, peak_idx: int) -> bool:
    """Return True if price was above its 40-week SMA at the pivot week."""
    if peak_idx < 39:
        return False  # insufficient history for SMA40
    closes = weekly["close"].to_numpy(dtype=float)
    sma40 = float(closes[peak_idx - 39 : peak_idx + 1].mean())
    return float(closes[peak_idx]) > sma40


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _deduplicate_pivots(
    pivots: list[dict[str, Any]],
    band_pct: float = _DEDUP_BAND,
) -> list[dict[str, Any]]:
    """Merge pivots whose prices are within band_pct of each other.

    Within each cluster, keep the pivot with the highest advance_pct
    (or the open base if no confirmed breakout exists in the cluster).
    """
    if not pivots:
        return []

    sorted_pivots = sorted(pivots, key=lambda p: p["pivot_price"])
    merged: list[dict[str, Any]] = [sorted_pivots[0]]

    for p in sorted_pivots[1:]:
        last = merged[-1]
        if (p["pivot_price"] - last["pivot_price"]) / last["pivot_price"] < band_pct:
            adv_p = p.get("advance_pct") or 0.0
            adv_last = last.get("advance_pct") or 0.0
            if adv_p > adv_last:
                merged[-1] = p
        else:
            merged.append(p)

    return sorted(merged, key=lambda p: p["base_start"])


# ---------------------------------------------------------------------------
# Open base detection (most recent base, not yet broken out)
# ---------------------------------------------------------------------------


def _detect_open_base(
    weekly: pd.DataFrame,
    confirmed_pivots: list[dict[str, Any]],
    max_depth_pct: float = _MAX_BASE_DEPTH,
    min_base_weeks: int = _MIN_BASE_WEEKS,
) -> dict[str, Any] | None:
    """Check whether the trailing price action forms an open (unbroken) base."""
    closes = weekly["close"].to_numpy(dtype=float)
    highs = weekly["high"].to_numpy(dtype=float)
    n = len(weekly)
    # Look back up to 52 weeks from the end
    lookback = min(n, 52)
    recent_slice = weekly["high"].iloc[n - lookback :]
    recent_high_label = recent_slice.idxmax()
    recent_high_pos = int(weekly.index.get_loc(recent_high_label))  # type: ignore[arg-type]

    pivot_price = float(highs[recent_high_pos])
    floor = pivot_price * (1.0 - max_depth_pct / 100.0)

    # Check price hasn't already broken out above this high
    post_closes = closes[recent_high_pos + 1 :]
    if (
        len(post_closes)
        and float(post_closes.max()) >= pivot_price * _BREAKOUT_THRESHOLD
    ):
        return None  # already broken out — will be caught by confirmed pivots

    # Walk back to find base start
    lows = weekly["low"].to_numpy(dtype=float)
    base_start_idx = recent_high_pos
    for i in range(recent_high_pos - 1, max(0, recent_high_pos - 104), -1):
        if lows[i] < floor:
            break
        base_start_idx = i

    base_weeks = recent_high_pos - base_start_idx
    if base_weeks < min_base_weeks:
        return None

    # Don't duplicate a confirmed pivot within 5%
    for cp in confirmed_pivots:
        if abs(cp["pivot_price"] - pivot_price) / pivot_price < _DEDUP_BAND:
            return None

    base_lows = lows[base_start_idx : recent_high_pos + 1]
    depth = (pivot_price - float(base_lows.min())) / pivot_price * 100.0

    return {
        "pivot_price": round(pivot_price, 2),
        "base_start": weekly.index[base_start_idx].date().isoformat(),
        "base_end": weekly.index[n - 1].date().isoformat(),
        "base_weeks": base_weeks,
        "max_depth_pct": round(depth, 1),
        "breakout_date": None,
        "breakout_close": None,
        "advance_pct": None,
        "stage2_at_pivot": _is_stage2_at_pivot(weekly, recent_high_pos),
        "status": "open",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def find_historical_pivots(
    ticker: str,
    period: str = _DEFAULT_PERIOD,
    half_window: int = _HALF_WINDOW,
    min_base_weeks: int = _MIN_BASE_WEEKS,
    max_depth_pct: float = _MAX_BASE_DEPTH,
    min_advance_pct: float = _MIN_ADVANCE_PCT,
    require_stage2: bool = True,
) -> list[dict[str, Any]]:
    """Return confirmed historical base pivot levels for a ticker, oldest first.

    Each dict contains:
        pivot_price, base_start, base_end, base_weeks, max_depth_pct,
        breakout_date, breakout_close, advance_pct, stage2_at_pivot, status
    """
    completed_week_start = _completed_week_start(ticker)
    key = _cache_key(
        ticker,
        period,
        half_window,
        min_base_weeks,
        max_depth_pct,
        min_advance_pct,
        require_stage2,
        completed_week_start,
    )
    cached, flight, is_leader = _get_cached_or_begin_flight(key)
    if cached is not None:
        return cached
    assert flight is not None
    if not is_leader:
        flight.ready.wait()
        if flight.error is not None:
            raise flight.error
        assert flight.result is not None
        return deepcopy(flight.result)

    try:
        weekly = fetch_weekly_ohlcv(ticker, period)
        weekly = _completed_weekly_bars(weekly, completed_week_start)
        if len(weekly) < _MIN_HISTORY_BARS:
            raise ValueError(
                f"Only {len(weekly)} completed weekly bars for {ticker}; "
                f"need ≥{_MIN_HISTORY_BARS}"
            )
        peak_indices = _find_peak_candidates(weekly, half_window)

        pivots: list[dict[str, Any]] = []
        for idx in peak_indices:
            pivot_price = round(float(weekly["high"].iloc[idx]), 2)

            if require_stage2 and not _is_stage2_at_pivot(weekly, idx):
                continue

            base = _build_base(weekly, idx, max_depth_pct, min_base_weeks)
            if base is None:
                continue

            breakout = _find_breakout(
                weekly,
                idx,
                float(weekly["high"].iloc[idx]),
                min_advance_pct=min_advance_pct,
            )

            pivots.append(
                {
                    "pivot_price": pivot_price,
                    **{
                        k: v
                        for k, v in base.items()
                        if k not in ("base_start_idx", "base_end_idx")
                    },
                    **breakout,
                    "stage2_at_pivot": _is_stage2_at_pivot(weekly, idx),
                }
            )

        confirmed = _deduplicate_pivots(pivots)

        open_base = _detect_open_base(weekly, confirmed, max_depth_pct, min_base_weeks)
        if open_base is not None:
            confirmed.append(open_base)
    except BaseException as exc:
        _finish_flight(key, flight, error=exc)
        raise

    _finish_flight(key, flight, result=confirmed)
    return confirmed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_pivots(ticker: str, pivots: list[dict[str, Any]]) -> None:
    """Pretty-print pivot table to stdout."""
    if not pivots:
        print(f"No historical pivots found for {ticker}")
        return

    print(f"\n{'-' * 80}")
    print(f"  Historical base pivots  {ticker.upper()}")
    print(f"{'-' * 80}")
    header = f"  {'Pivot':>8}  {'Base start':<12}  {'Base end':<12}  {'Wks':>4}  "
    header += f"{'Depth':>6}  {'Breakout date':<14}  {'Breakout $':>10}  {'Advance':>8}  Status"
    print(header)
    print(f"  {'-' * 74}")

    status_labels = {
        "confirmed": "Confirmed",
        "failed": "Failed bo",
        "resistance": "Resistance",
        "open": "OPEN BASE",
    }
    for p in pivots:
        advance = (
            f"{p['advance_pct']:.1f}%" if p.get("advance_pct") is not None else "--"
        )
        bo_date = p.get("breakout_date") or "--"
        bo_close = f"${p['breakout_close']:.2f}" if p.get("breakout_close") else "--"
        label = status_labels.get(p.get("status", ""), p.get("status", ""))
        s2 = "" if p.get("stage2_at_pivot") else " (non-S2)"
        print(
            f"  ${p['pivot_price']:>7.2f}  {p['base_start']:<12}  {p['base_end']:<12}  "
            f"{p['base_weeks']:>4}  {p['max_depth_pct']:>5.1f}%  {bo_date:<14}  "
            f"{bo_close:>10}  {advance:>8}  {label}{s2}"
        )

    print(f"{'-' * 80}\n")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Find historical base pivot/breakout levels for a ticker"
    )
    parser.add_argument("ticker", help="Stock ticker symbol (e.g. WELL)")
    parser.add_argument(
        "--period", default=_DEFAULT_PERIOD, help="History period (e.g. 7y, 10y)"
    )
    parser.add_argument(
        "--no-stage2", action="store_true", help="Include non-Stage-2 bases"
    )
    parser.add_argument(
        "--min-advance",
        type=float,
        default=_MIN_ADVANCE_PCT,
        help="Minimum %% advance after breakout to confirm (default 8)",
    )
    parser.add_argument(
        "--max-depth",
        type=float,
        default=_MAX_BASE_DEPTH,
        help="Maximum base depth %% (default 35)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output as JSON instead of table",
    )
    args = parser.parse_args()

    pivots = find_historical_pivots(
        ticker=args.ticker.upper(),
        period=args.period,
        require_stage2=not args.no_stage2,
        min_advance_pct=args.min_advance,
        max_depth_pct=args.max_depth,
    )

    if args.as_json:
        print(json.dumps(pivots, indent=2))
    else:
        _print_pivots(args.ticker.upper(), pivots)


if __name__ == "__main__":
    main()
