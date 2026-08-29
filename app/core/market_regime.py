"""Shared market-regime helper — single source of truth for ``spy_uptrend``.

A pure core (:func:`evaluate_market_regime`) computes the SMA-above check and a
~52-week return over a close-price series with no network access, and a thin
wrapper (:func:`fetch_spy_market_regime`) downloads SPY history and delegates to
it. Behaviour is byte-identical to the scanner's former inline
``_fetch_spy_context`` computation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

import pandas as pd
import yfinance as yf

MA_LENGTH = 200
RETURN_LOOKBACK_SESSIONS = 252
SPY_FETCH_WINDOW_DAYS = 252 + 60


@dataclass(frozen=True)
class MarketRegimeReadingV1:
    """Immutable market-regime reading.

    ``spy_uptrend`` and ``return_52w_pct`` are what #386 consumes; ``sma_200``,
    ``latest_close``, ``session_count`` and ``is_degraded`` are provided for
    downstream callers (#387, #388).
    """

    spy_uptrend: bool
    return_52w_pct: float
    sma_200: float | None
    latest_close: float | None
    session_count: int
    is_degraded: bool


def _degraded_reading(session_count: int) -> MarketRegimeReadingV1:
    """Return the safe fallback reading used on empty/short/failed data."""
    return MarketRegimeReadingV1(
        spy_uptrend=True,
        return_52w_pct=0.0,
        sma_200=None,
        latest_close=None,
        session_count=session_count,
        is_degraded=True,
    )


def evaluate_market_regime(
    closes: Sequence[float] | pd.Series,
) -> MarketRegimeReadingV1:
    """Evaluate the market regime for a close-price series.

    Pure: performs no I/O and never raises. Non-finite values (NaN, inf) are
    dropped first. Fewer than :data:`MA_LENGTH` finite closes yields a degraded
    reading. Otherwise ``sma_200`` is the mean of the final ``MA_LENGTH``
    closes, ``spy_uptrend`` is a strict ``latest_close > sma_200``, and
    ``return_52w_pct`` is the percentage return from
    ``closes[max(0, n - RETURN_LOOKBACK_SESSIONS)]`` to the latest close.
    """
    values = [float(v) for v in list(closes) if math.isfinite(float(v))]
    n = len(values)
    if n < MA_LENGTH:
        return _degraded_reading(n)
    sma_200 = sum(values[-MA_LENGTH:]) / MA_LENGTH
    latest_close = values[-1]
    oldest = values[max(0, n - RETURN_LOOKBACK_SESSIONS)]
    return_52w_pct = round((latest_close / oldest - 1) * 100, 2)
    return MarketRegimeReadingV1(
        spy_uptrend=latest_close > sma_200,
        return_52w_pct=return_52w_pct,
        sma_200=sma_200,
        latest_close=latest_close,
        session_count=n,
        is_degraded=False,
    )


def fetch_spy_market_regime() -> MarketRegimeReadingV1:
    """Download SPY history and evaluate the market regime.

    Wraps the whole body in ``try/except Exception``; any failure, an empty
    download, or fewer than 200 rows returns the degraded fallback reading.
    """
    try:
        end = datetime.today()
        start = end - timedelta(days=SPY_FETCH_WINDOW_DAYS)
        df = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
        if df.empty or len(df) < 200:
            return _degraded_reading(len(df))
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"] if "Close" in df.columns else df["close"]
        return evaluate_market_regime(close)
    except Exception:
        return _degraded_reading(0)
