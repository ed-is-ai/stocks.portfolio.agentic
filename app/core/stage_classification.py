"""Dependency-neutral Weinstein Stage classification shared by all producers."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence, TypeVar


NumberT = TypeVar("NumberT", float, Decimal)
Stage = str


def sma_slope(
    prices: Sequence[NumberT], window: int, lookback: int = 4
) -> NumberT | None:
    """Approximate weekly SMA slope; positive means rising."""
    if window <= 0 or lookback <= 0 or len(prices) < window + lookback:
        return None
    current_values = prices[-window:]
    past_values = prices[-(window + lookback) : -lookback]
    current_sma = sum(current_values[1:], current_values[0]) / window
    past_sma = sum(past_values[1:], past_values[0]) / window
    return current_sma - past_sma


def classify_weinstein_stage(
    *,
    price: NumberT,
    sma150: NumberT | None,
    sma200: NumberT | None,
    price_history: Sequence[NumberT],
) -> Stage:
    """Apply the established Analyst Stage 1–4 rules without agent imports."""
    # Preserve Analyst's historical ``stock.smaX or price`` fallback exactly.
    effective_sma150 = price if sma150 is None or sma150 == 0 else sma150
    effective_sma200 = price if sma200 is None or sma200 == 0 else sma200

    above_150 = price > effective_sma150
    above_200 = price > effective_sma200
    ma_bullish = effective_sma150 > effective_sma200

    slope150 = sma_slope(price_history, window=30, lookback=4)
    slope200 = sma_slope(price_history, window=40, lookback=4)

    if slope150 is None or slope200 is None:
        if above_150 and ma_bullish:
            return "Stage 2"
        if not above_150 and not above_200:
            return "Stage 4"
        if above_200 and not above_150:
            return "Stage 3"
        return "Stage 1"

    zero = price - price
    if above_150 and ma_bullish and slope200 > zero:
        return "Stage 2"
    if not above_150 and not above_200 and slope150 < zero:
        return "Stage 4"
    if (not above_150 and above_200) or (above_150 and ma_bullish and slope200 <= zero):
        return "Stage 3"
    return "Stage 1"


__all__ = ["Stage", "classify_weinstein_stage", "sma_slope"]
