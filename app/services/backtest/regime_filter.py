"""Shared, runtime-safe market-regime entry filter for backtest strategies.

One implementation of the "is the benchmark above its N-session simple
moving average" check, evaluated from a :class:`MarketViewV1` benchmark's
``price_history``. Every ``kind: backtest-strategy`` skill receives the
opt-in parameter set via discovery-time injection and calls
:func:`entry_signals_permitted` as a single guard clause at the top of its
``entry_signals`` method. ``exit_signals`` and ``position_size`` are never
touched.

This module re-expresses the same "latest close vs trailing SMA, drop
non-finite" math as ``app.core.market_regime.evaluate_market_regime`` but
against a benchmark security's :class:`MarketViewV1` history, with a
caller-supplied MA length. It deliberately stays inside the Strategy
runtime import allowlist: it imports only stdlib and
``app.services.backtest.strategy_protocol`` and duck-types the
``price_history`` frame the way the existing strategies already do.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from app.services.backtest.strategy_protocol import MarketViewV1, StrategyParameters

#: Opt-in flag; entries are only gated when this parameter is exactly ``True``.
REGIME_FILTER_ENABLED_PARAM = "regime_filter_enabled"

#: Canonical id of the benchmark security whose regime governs the gate.
REGIME_FILTER_BENCHMARK_PARAM = "regime_filter_benchmark_security_id"

#: Length, in sessions, of the benchmark's trailing simple moving average.
REGIME_FILTER_MA_LENGTH_PARAM = "regime_filter_ma_length"

#: Smallest meaningful moving-average length; anything below fails closed.
MIN_MA_LENGTH = 2


def _finite_closes(history: Any) -> list[float]:
    """Return the frame's finite ``close`` values, or ``[]`` on any mismatch.

    Mirrors how the existing strategies read a frame: an ``.empty`` guard,
    a ``"close" in .columns`` membership check, then ``.tolist()``. A frame
    shape problem or missing column yields an empty list, which the caller
    treats as insufficient history (fail closed). Individual cells that are
    non-numeric or non-finite (``NaN``/``inf``) are dropped one at a time,
    matching ``evaluate_market_regime`` -- one dirty tick never discards the
    whole series.
    """
    try:
        if history is None or history.empty:
            return []
        if "close" not in history.columns:
            return []
        raw = history["close"].tolist()
    except (AttributeError, TypeError, ValueError, KeyError):
        return []
    closes: list[float] = []
    for value in raw:
        try:
            number = float(value)
        except (ArithmeticError, TypeError, ValueError):
            continue
        if math.isfinite(number):
            closes.append(number)
    return closes


def entry_signals_permitted(
    view: MarketViewV1,
    parameters: StrategyParameters,
    universe: Sequence[str],
) -> bool:
    """Return whether entry signals are permitted for the current session.

    Returns ``True`` immediately unless
    ``parameters[REGIME_FILTER_ENABLED_PARAM]`` is exactly ``True`` -- so a
    Run that omits every ``regime_filter_*`` key, or sets the flag to
    anything other than ``True``, sees behaviour byte-identical to the
    pre-change strategy.

    When enabled, the gate fails closed (returns ``False``, never assuming
    risk-on) whenever the benchmark id is empty / not a ``str`` / not in
    ``universe``; the MA length is a ``bool``, a non-``int``, or
    ``< MIN_MA_LENGTH``; ``view.price_history`` raises; or the benchmark
    yields fewer finite closes than the MA length. Otherwise it returns
    ``latest_close > trailing_sma``.
    """
    if parameters.get(REGIME_FILTER_ENABLED_PARAM) is not True:
        return True

    benchmark = parameters.get(REGIME_FILTER_BENCHMARK_PARAM)
    selected = (
        tuple(universe) if isinstance(universe, (list, tuple, set, frozenset)) else ()
    )
    if not isinstance(benchmark, str) or not benchmark or benchmark not in selected:
        return False

    ma_length = parameters.get(REGIME_FILTER_MA_LENGTH_PARAM)
    if (
        isinstance(ma_length, bool)
        or not isinstance(ma_length, int)
        or ma_length < MIN_MA_LENGTH
    ):
        return False

    try:
        history = view.price_history(benchmark)
    except Exception:
        return False

    closes = _finite_closes(history)
    if len(closes) < ma_length:
        return False

    sma = sum(closes[-ma_length:]) / ma_length
    return closes[-1] > sma
