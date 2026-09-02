"""Deterministic long-only simple moving-average crossover strategy."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

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

STRATEGY_ID = "rtly-backtest-moving-average"
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


def _fresh_history(view: MarketViewV1, security_id: str) -> Any | None:
    try:
        history = view.price_history(security_id)
        if history is None or len(history.index) == 0:
            return None
        latest_session = _as_date(history.index[-1])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    return history if latest_session == view.as_of_session else None


def _finite_decimal(value: object) -> Decimal | None:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _close_values(history: Any) -> list[Decimal] | None:
    try:
        raw_values = list(history["close"])
    except (KeyError, TypeError, AttributeError):
        return None
    values = [_finite_decimal(value) for value in raw_values]
    if any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


def _windows(parameters: StrategyParameters) -> tuple[int, int] | None:
    fast = parameters.get("fast_window", 50)
    slow = parameters.get("slow_window", 200)
    if (
        isinstance(fast, bool)
        or not isinstance(fast, int)
        or isinstance(slow, bool)
        or not isinstance(slow, int)
        or fast < 1
        or slow < 2
        or fast >= slow
    ):
        return None
    return fast, slow


class _CrossoverReading(NamedTuple):
    """The crossover verdict plus the moving averages that produced it."""

    direction: int
    fast_window: int
    slow_window: int
    previous_fast: Decimal
    previous_slow: Decimal
    current_fast: Decimal
    current_slow: Decimal


def _crossover(
    view: MarketViewV1, parameters: StrategyParameters, security_id: str
) -> _CrossoverReading | None:
    """Return today's crossover reading, or ``None`` without enough evidence.

    The decision itself is unchanged; the computed averages ride along so
    the emitted signal can explain itself (#472).
    """
    windows = _windows(parameters)
    history = _fresh_history(view, security_id)
    if windows is None or history is None:
        return None
    fast, slow = windows
    closes = _close_values(history)
    if closes is None or len(closes) < slow + 1:
        return None

    previous_fast = sum(closes[-fast - 1 : -1]) / Decimal(fast)
    previous_slow = sum(closes[-slow - 1 : -1]) / Decimal(slow)
    current_fast = sum(closes[-fast:]) / Decimal(fast)
    current_slow = sum(closes[-slow:]) / Decimal(slow)
    if previous_fast <= previous_slow and current_fast > current_slow:
        direction = 1
    elif previous_fast >= previous_slow and current_fast < current_slow:
        direction = -1
    else:
        direction = 0
    return _CrossoverReading(
        direction=direction,
        fast_window=fast,
        slow_window=slow,
        previous_fast=previous_fast,
        previous_slow=previous_slow,
        current_fast=current_fast,
        current_slow=current_slow,
    )


def _crossover_explanation(
    reading: _CrossoverReading, session: date
) -> SignalExplanationV1:
    """Explain one bullish/bearish SMA crossover in shared, generic terms."""
    bullish = reading.direction == 1
    return SignalExplanationV1(
        reasons=[
            SignalReasonV1(
                code="bullish_ma_crossover" if bullish else "bearish_ma_crossover",
                summary=(
                    "The fast moving average crossed above the slow moving average."
                    if bullish
                    else "The fast moving average crossed below the slow "
                    "moving average."
                ),
                facts=[
                    ExplanationFactV1(
                        label="Fast moving average",
                        observed=reading.current_fast,
                        operator=(
                            ComparisonOperator.CROSSED_ABOVE
                            if bullish
                            else ComparisonOperator.CROSSED_BELOW
                        ),
                        threshold=reading.current_slow,
                        unit=EvidenceUnit.PRICE,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Previous fast moving average",
                        observed=reading.previous_fast,
                        unit=EvidenceUnit.PRICE,
                    ),
                    ExplanationFactV1(
                        label="Previous slow moving average",
                        observed=reading.previous_slow,
                        unit=EvidenceUnit.PRICE,
                    ),
                    ExplanationFactV1(
                        label="Fast window",
                        observed=Decimal(reading.fast_window),
                        unit=EvidenceUnit.SESSIONS,
                    ),
                    ExplanationFactV1(
                        label="Slow window",
                        observed=Decimal(reading.slow_window),
                        unit=EvidenceUnit.SESSIONS,
                    ),
                ],
            ),
        ]
    )


def _directional_signal(
    view: MarketViewV1,
    parameters: StrategyParameters,
    security_id: str,
    *,
    direction: int,
) -> Signal | None:
    """Return the crossover signal for ``direction``, or ``None``."""
    reading = _crossover(view, parameters, security_id)
    if reading is None or reading.direction != direction:
        return None
    bullish = direction == 1
    return Signal(
        security_id=security_id,
        side=SignalSide.BUY if bullish else SignalSide.SELL,
        session=view.as_of_session,
        rule_id=(
            "moving_average_bullish_crossover_v1"
            if bullish
            else "moving_average_bearish_crossover_v1"
        ),
        explanation=_crossover_explanation(reading, view.as_of_session),
    )


class MovingAverageStrategy:
    """Emit signals only on a true fast/slow SMA crossover."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare ``slow_window + 1`` closes — the crossover's own guard."""
        windows = _windows(parameters)
        slow = 200 if windows is None else windows[1]
        history = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY,
            minimum_sessions=slow + 1,
            columns=("close",),
        )
        return StrategyEvidenceRequirementsV1(entry=(history,), exit=(history,))

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        universe = _universe(parameters)
        if not entry_signals_permitted(view, parameters, universe):
            return []
        signals = [
            _directional_signal(view, parameters, security_id, direction=1)
            for security_id in universe
        ]
        return [signal for signal in signals if signal is not None]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        held = {
            position.security_id
            for position in portfolio.positions
            if position.quantity > 0
        }
        signals = [
            _directional_signal(view, parameters, security_id, direction=-1)
            for security_id in _universe(parameters)
            if security_id in held
        ]
        return [signal for signal in signals if signal is not None]

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
