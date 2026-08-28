"""Deterministic passive buy-and-hold benchmark strategy."""

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

STRATEGY_ID = "rtly-backtest-buy-and-hold"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    """Return the host-bound selected universe as a canonical ID tuple.

    The host injects an already sorted, deduplicated tuple; re-deriving it
    here keeps iteration deterministic for any caller and makes a
    malformed or empty universe fail closed with no signals.
    """
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    date_method = getattr(value, "date", None)
    if callable(date_method):
        try:
            result = date_method()
        except (TypeError, ValueError, OverflowError):
            return None
        return result if isinstance(result, date) else None
    return None


def _has_fresh_history(view: MarketViewV1, security_id: str) -> bool:
    try:
        history: Any = view.price_history(security_id)
        if history is None or len(history.index) == 0:
            return False
        latest_session = _as_date(history.index[-1])
        current = history.iloc[-1]
        close = Decimal(str(current["close"]))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return False
    except InvalidOperation:
        return False
    return latest_session == view.as_of_session and close.is_finite() and close > 0


def _cutoff(parameters: StrategyParameters) -> date | None:
    raw = parameters.get("entry_on_or_after", "2000-01-01")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


class BuyAndHoldStrategy:
    """Emit repeat BUY candidates from a configured cutoff and never SELL."""

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        cutoff = _cutoff(parameters)
        if cutoff is None or view.as_of_session < cutoff:
            return []
        return [
            Signal(
                security_id=security_id,
                side=SignalSide.BUY,
                session=view.as_of_session,
                rule_id="buy_and_hold_entry_v1",
            )
            for security_id in _universe(parameters)
            if _has_fresh_history(view, security_id)
        ]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        return []

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        if signal.side == SignalSide.BUY:
            # The engine reserves equal capital and determines whole shares.
            return 0
        for position in portfolio.positions:
            if position.security_id == signal.security_id:
                integral = position.quantity.to_integral_value()
                return int(integral) if position.quantity == integral else 0
        return 0
