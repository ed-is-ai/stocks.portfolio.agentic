"""Deterministic long-only Darvas box Strategy for bounded backtests."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-darvas-box"
STRATEGY_API_VERSION = 1


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _session_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    converter = getattr(value, "date", None)
    if callable(converter):
        converted = converter()
        return converted if isinstance(converted, date) else None
    return None


def _bounded_history(view: MarketViewV1, security_id: str) -> Any | None:
    history = view.price_history(security_id)
    if history is None or getattr(history, "empty", True):
        return None
    try:
        latest_session = _session_date(history.index[-1])
    except (IndexError, KeyError, TypeError):
        return None
    return history if latest_session == view.as_of_session else None


def _plain_int(parameters: StrategyParameters, name: str) -> int | None:
    value = parameters.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _held_quantity(portfolio: PortfolioView, security_id: str) -> int:
    for position in portfolio.positions:
        if position.security_id != security_id:
            continue
        quantity = position.quantity
        if quantity <= 0 or quantity != quantity.to_integral_value():
            return 0
        return int(quantity)
    return 0


class DarvasBoxStrategy:
    """Buy strict prior-box breakouts and sell strict box-bottom breaks."""

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        security_id = str(parameters.get("security_id", ""))
        lookback = _plain_int(parameters, "box_lookback_sessions")
        maximum_depth = _decimal(parameters.get("maximum_box_depth_pct"))
        volume_multiplier = _decimal(parameters.get("volume_multiplier"))
        history = _bounded_history(view, security_id)
        if (
            not security_id
            or lookback is None
            or lookback < 1
            or maximum_depth is None
            or volume_multiplier is None
            or history is None
            or len(history) < lookback + 1
        ):
            return []

        prior = history.iloc[-lookback - 1 : -1]
        current = history.iloc[-1]
        try:
            highs = [_decimal(value) for value in prior["high"]]
            lows = [_decimal(value) for value in prior["low"]]
            volumes = [_decimal(value) for value in prior["volume"]]
            current_close = _decimal(current["close"])
            current_volume = _decimal(current["volume"])
        except (KeyError, TypeError):
            return []
        if (
            any(value is None for value in highs + lows + volumes)
            or current_close is None
            or current_volume is None
        ):
            return []

        valid_highs = [value for value in highs if value is not None]
        valid_lows = [value for value in lows if value is not None]
        valid_volumes = [value for value in volumes if value is not None]
        box_top = max(valid_highs)
        box_bottom = min(valid_lows)
        if box_top <= 0 or box_bottom < 0:
            return []
        box_depth_pct = (box_top - box_bottom) * Decimal(100) / box_top
        mean_volume = sum(valid_volumes, Decimal(0)) / Decimal(lookback)
        if (
            mean_volume <= 0
            or current_volume < 0
            or box_depth_pct > maximum_depth
            or current_close <= box_top
            or current_volume < mean_volume * volume_multiplier
        ):
            return []
        return [
            Signal(
                security_id=security_id,
                side=SignalSide.BUY,
                session=view.as_of_session,
                rule_id="darvas_box_breakout_v1",
            )
        ]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        security_id = str(parameters.get("security_id", ""))
        if _held_quantity(portfolio, security_id) == 0:
            return []
        lookback = _plain_int(parameters, "box_lookback_sessions")
        history = _bounded_history(view, security_id)
        if (
            lookback is None
            or lookback < 1
            or history is None
            or len(history) < lookback + 1
        ):
            return []
        prior = history.iloc[-lookback - 1 : -1]
        current = history.iloc[-1]
        try:
            lows = [_decimal(value) for value in prior["low"]]
            current_close = _decimal(current["close"])
        except (KeyError, TypeError):
            return []
        if any(value is None for value in lows) or current_close is None:
            return []
        box_bottom = min(value for value in lows if value is not None)
        if current_close >= box_bottom:
            return []
        return [
            Signal(
                security_id=security_id,
                side=SignalSide.SELL,
                session=view.as_of_session,
                rule_id="darvas_box_breakdown_v1",
            )
        ]

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        if signal.side == SignalSide.SELL:
            return _held_quantity(portfolio, signal.security_id)
        fixed_shares = _plain_int(parameters, "fixed_shares")
        return fixed_shares if fixed_shares is not None and fixed_shares > 0 else 0


strategy = DarvasBoxStrategy()
