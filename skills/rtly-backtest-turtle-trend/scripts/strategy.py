"""Deterministic long-only Turtle channel Strategy for bounded backtests."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.backtest.regime_filter import entry_signals_permitted
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_explanation import (
    ComparisonOperator,
    EvidenceUnit,
    ExplanationFactV1,
    SignalExplanationV1,
    SignalReasonV1,
)
from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-turtle-trend"
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


def _channel_values(
    history: Any, column: str, lookback: int
) -> tuple[list[Decimal], Decimal] | None:
    if len(history) < lookback + 1:
        return None
    prior = history.iloc[-lookback - 1 : -1]
    current = history.iloc[-1]
    try:
        prior_values = [_decimal(value) for value in prior[column]]
        current_value = _decimal(current[column])
    except (KeyError, TypeError):
        return None
    if any(value is None for value in prior_values) or current_value is None:
        return None
    return [value for value in prior_values if value is not None], current_value


class TurtleTrendStrategy:
    """Buy strict high-channel breaks and sell strict low-channel breaches."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare each channel's own ``lookback + 1`` session window."""
        entry_lookback = _plain_int(parameters, "entry_lookback_sessions") or 20
        exit_lookback = _plain_int(parameters, "exit_lookback_sessions") or 10
        return StrategyEvidenceRequirementsV1(
            entry=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=max(entry_lookback, 1) + 1,
                    columns=("high",),
                ),
            ),
            exit=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=max(exit_lookback, 1) + 1,
                    columns=("low",),
                ),
            ),
        )

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        universe = _universe(parameters)
        if not entry_signals_permitted(view, parameters, universe):
            return []
        signals = [
            self._entry_signal(view, parameters, security_id)
            for security_id in universe
        ]
        return [signal for signal in signals if signal is not None]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        signals = [
            self._exit_signal(view, portfolio, parameters, security_id)
            for security_id in _universe(parameters)
        ]
        return [signal for signal in signals if signal is not None]

    def _entry_signal(
        self, view: MarketViewV1, parameters: StrategyParameters, security_id: str
    ) -> Signal | None:
        lookback = _plain_int(parameters, "entry_lookback_sessions")
        history = _bounded_history(view, security_id)
        if lookback is None or lookback < 1 or history is None:
            return None
        values = _channel_values(history, "high", lookback)
        if values is None:
            return None
        prior_highs, current_high = values
        channel_high = max(prior_highs)
        if current_high <= channel_high:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=view.as_of_session,
            rule_id="turtle_entry_channel_breakout_v1",
            explanation=SignalExplanationV1(
                reasons=[
                    SignalReasonV1(
                        code="channel_breakout",
                        summary=(
                            "Today's high broke above the prior entry "
                            "channel's highest high."
                        ),
                        facts=[
                            ExplanationFactV1(
                                label="High",
                                observed=current_high,
                                operator=ComparisonOperator.GT,
                                threshold=channel_high,
                                unit=EvidenceUnit.PRICE,
                                as_of=view.as_of_session,
                            ),
                            ExplanationFactV1(
                                label="Entry channel lookback",
                                observed=Decimal(lookback),
                                unit=EvidenceUnit.SESSIONS,
                            ),
                        ],
                    ),
                ]
            ),
        )

    def _exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> Signal | None:
        if _held_quantity(portfolio, security_id) == 0:
            return None
        lookback = _plain_int(parameters, "exit_lookback_sessions")
        history = _bounded_history(view, security_id)
        if lookback is None or lookback < 1 or history is None:
            return None
        values = _channel_values(history, "low", lookback)
        if values is None:
            return None
        prior_lows, current_low = values
        channel_low = min(prior_lows)
        if current_low >= channel_low:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id="turtle_exit_channel_breach_v1",
            explanation=SignalExplanationV1(
                reasons=[
                    SignalReasonV1(
                        code="channel_breach",
                        summary=(
                            "Today's low breached the prior exit channel's lowest low."
                        ),
                        facts=[
                            ExplanationFactV1(
                                label="Low",
                                observed=current_low,
                                operator=ComparisonOperator.LT,
                                threshold=channel_low,
                                unit=EvidenceUnit.PRICE,
                                as_of=view.as_of_session,
                            ),
                            ExplanationFactV1(
                                label="Exit channel lookback",
                                observed=Decimal(lookback),
                                unit=EvidenceUnit.SESSIONS,
                            ),
                        ],
                    ),
                ]
            ),
        )

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        if signal.side == SignalSide.SELL:
            return _held_quantity(portfolio, signal.security_id)
        # The engine reserves equal capital and determines whole shares.
        return 0


strategy = TurtleTrendStrategy()
