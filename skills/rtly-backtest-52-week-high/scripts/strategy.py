"""Deterministic long-only 52-week-high proximity Strategy."""

from __future__ import annotations

from collections.abc import Mapping
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
    StopLevelV1,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-52-week-high"
STRATEGY_API_VERSION = 1
UNIVERSE_PARAMETER = "selected_securities"
DEFAULT_LOOKBACK = 252
CADENCES = frozenset({"daily", "monthly"})
_ENTRY_RULE = "fifty_two_week_high_entry_v1"
_EXIT_RULE = "fifty_two_week_high_exit_v1"
_READ_ERRORS = (AttributeError, IndexError, KeyError, TypeError, ValueError)


class _Settings(NamedTuple):
    """The validated parameter set; ``None`` from :func:`_settings` if unusable."""

    lookback: int
    entry_proximity: Decimal
    exit_proximity: Decimal
    cadence: str
    maximum_loss_pct: Decimal


class _Reading(NamedTuple):
    """One security's proximity (if computable) and the session before today."""

    proximity: Decimal | None
    high: Decimal | None
    previous_session: date | None


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    """Return the selected universe sorted and deduplicated, or ``()``."""
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _lookback(parameters: StrategyParameters) -> int | None:
    value = parameters.get("high_lookback_sessions")
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        return None
    return value


def _settings(parameters: StrategyParameters) -> _Settings | None:
    """Validate every setting; any missing or inconsistent one fails closed.

    ``exit_proximity >= entry_proximity`` is refused: it would let the same
    reading both enter and exit a position.
    """
    lookback = _lookback(parameters)
    entry = _decimal(parameters.get("entry_proximity"))
    exit_ = _decimal(parameters.get("exit_proximity"))
    loss = _decimal(parameters.get("maximum_loss_pct"))
    cadence = parameters.get("rebalance_cadence")
    if lookback is None or entry is None or exit_ is None or loss is None:
        return None
    if not isinstance(cadence, str) or cadence not in CADENCES:
        return None
    if not (0 < entry <= 1 and 0 <= exit_ < entry and 0 <= loss < 100):
        return None
    return _Settings(lookback, entry, exit_, cadence, loss)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    converter = getattr(value, "date", None)
    if callable(converter):
        try:
            result = converter()
        except (TypeError, ValueError, OverflowError):
            return None
        return result if isinstance(result, date) else None
    return None


def _read(view: MarketViewV1, security_id: str, lookback: int) -> _Reading | None:
    """Return the reading, or ``None`` without a bar on the as-of session.

    The view may return more rows than ``limit``, so the window is the tail.
    """
    try:
        history = view.price_history(
            security_id, limit=lookback, columns=("high", "close")
        )
        if history is None or len(history.index) == 0:
            return None
        if _as_date(history.index[-1]) != view.as_of_session:
            return None
        previous = _as_date(history.index[-2]) if len(history.index) > 1 else None
        window = history.tail(lookback)
        close = _decimal(window["close"].iloc[-1])
        highs = [_decimal(value) for value in window["high"]]
    except _READ_ERRORS:
        return None
    usable = [high for high in highs if high is not None and high > 0]
    if close is None or close <= 0 or len(usable) != lookback:
        return _Reading(None, None, previous)
    high = max(usable)
    return _Reading(min(close / high, Decimal(1)), high, previous)


def _readings(
    view: MarketViewV1, ids: tuple[str, ...], lookback: int
) -> dict[str, _Reading]:
    found = {sid: _read(view, sid, lookback) for sid in ids}
    return {sid: reading for sid, reading in found.items() if reading is not None}


def _is_rebalance(
    view: MarketViewV1, readings: Mapping[str, _Reading], cadence: str
) -> bool:
    """True every session for ``daily``; first session of a month for ``monthly``.

    The prior session is the latest one any fresh security traded, so a
    single security's gap cannot fake a rebalance.
    """
    if cadence == "daily":
        return True
    previous = max(
        (r.previous_session for r in readings.values() if r.previous_session),
        default=None,
    )
    if previous is None:
        return False
    today = view.as_of_session
    return (today.year, today.month) != (previous.year, previous.month)


def _current_close(view: MarketViewV1, security_id: str) -> Decimal | None:
    """Return the close on ``view.as_of_session``, or ``None`` without one."""
    try:
        history = view.price_history(security_id, limit=1, columns=("close",))
        if history is None or len(history.index) == 0:
            return None
        if _as_date(history.index[-1]) != view.as_of_session:
            return None
        close = _decimal(history["close"].iloc[-1])
        return close if close is not None and close > 0 else None
    except _READ_ERRORS:
        return None


def _held(portfolio: PortfolioView, security_id: str) -> Any | None:
    return next(
        (
            position
            for position in portfolio.positions
            if position.security_id == security_id and position.quantity > 0
        ),
        None,
    )


def _proximity_reason(
    view: MarketViewV1, buy: bool, reading: _Reading, threshold: Decimal
) -> SignalReasonV1:
    return SignalReasonV1(
        code="near_52_week_high" if buy else "proximity_below_exit",
        summary=(
            "Close is near the highest high of the lookback window."
            if buy
            else "Close fell below the exit proximity to the window high."
        ),
        facts=[
            ExplanationFactV1(
                label="Proximity to high",
                observed=reading.proximity,
                operator=ComparisonOperator.GTE if buy else ComparisonOperator.LT,
                threshold=threshold,
                unit=EvidenceUnit.RATIO,
                as_of=view.as_of_session,
            ),
            ExplanationFactV1(
                label="Window high",
                observed=reading.high,
                unit=EvidenceUnit.PRICE,
                as_of=view.as_of_session,
            ),
        ],
    )


def _stop_reason(
    view: MarketViewV1, close: Decimal, stop: Decimal, loss: Decimal
) -> SignalReasonV1:
    return SignalReasonV1(
        code="maximum_loss_stop",
        summary="Close hit the maximum-loss stop for this position.",
        facts=[
            ExplanationFactV1(
                label="Close",
                observed=close,
                operator=ComparisonOperator.LTE,
                threshold=stop,
                unit=EvidenceUnit.PRICE,
                as_of=view.as_of_session,
            ),
            ExplanationFactV1(
                label="Maximum loss", observed=loss, unit=EvidenceUnit.PERCENT
            ),
        ],
    )


def _stop_price(average_cost: Decimal, loss: Decimal) -> Decimal:
    return average_cost * (Decimal(1) - loss / Decimal(100))


class FiftyTwoWeekHighStrategy:
    """Buy closes near the window high; sell on falling away or the risk stop."""

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare ``high_lookback_sessions`` rows of highs and closes."""
        history = EvidenceRequirementV1(
            kind=EvidenceKind.PRICE_HISTORY,
            minimum_sessions=_lookback(parameters) or DEFAULT_LOOKBACK,
            columns=("high", "close"),
        )
        return StrategyEvidenceRequirementsV1(entry=(history,), exit=(history,))

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        """BUY every security at or above ``entry_proximity`` on a rebalance."""
        universe = _universe(parameters)
        if not entry_signals_permitted(view, parameters, universe):
            return []
        settings = _settings(parameters)
        if settings is None:
            return []
        readings = _readings(view, universe, settings.lookback)
        if not _is_rebalance(view, readings, settings.cadence):
            return []
        signals: list[Signal] = []
        for security_id in universe:
            reading = readings.get(security_id)
            if reading is None or reading.proximity is None:
                continue
            if reading.proximity < settings.entry_proximity:
                continue
            reason = _proximity_reason(view, True, reading, settings.entry_proximity)
            signals.append(
                Signal(
                    security_id=security_id,
                    side=SignalSide.BUY,
                    session=view.as_of_session,
                    rule_id=_ENTRY_RULE,
                    priority=reading.proximity,
                    explanation=SignalExplanationV1(reasons=[reason]),
                )
            )
        return signals

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        """Risk stop every session; proximity exit on a rebalance session."""
        settings = _settings(parameters)
        if settings is None:
            return []
        universe = _universe(parameters)
        held = [(sid, _held(portfolio, sid)) for sid in universe]
        held = [(sid, position) for sid, position in held if position is not None]
        if not held:
            return []
        readings = _readings(view, universe, settings.lookback)
        rebalance = _is_rebalance(view, readings, settings.cadence)
        signals: list[Signal] = []
        for security_id, position in held:
            reasons: list[SignalReasonV1] = []
            close = _current_close(view, security_id)
            stop = _stop_price(position.average_cost, settings.maximum_loss_pct)
            if close is not None and close <= stop:
                reasons.append(
                    _stop_reason(view, close, stop, settings.maximum_loss_pct)
                )
            reading = readings.get(security_id)
            if (
                rebalance
                and reading is not None
                and reading.proximity is not None
                and reading.proximity < settings.exit_proximity
            ):
                reasons.append(
                    _proximity_reason(view, False, reading, settings.exit_proximity)
                )
            if reasons:
                signals.append(
                    Signal(
                        security_id=security_id,
                        side=SignalSide.SELL,
                        session=view.as_of_session,
                        rule_id=_EXIT_RULE,
                        explanation=SignalExplanationV1(reasons=reasons),
                    )
                )
        return signals

    def stop_level(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> StopLevelV1 | None:
        """Return the cost-based close at which the risk stop fires.

        Settings are validated as the exit validates them, with no defaults,
        so an advertised level is always one the exit would act on.
        """
        held = _held(portfolio, security_id)
        if held is None:
            return None
        settings = _settings(parameters)
        if settings is None:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary="Strategy settings are missing, unusable or inconsistent.",
            )
        loss = settings.maximum_loss_pct
        stop = _stop_price(held.average_cost, loss)
        facts = [
            ExplanationFactV1(
                label="Maximum-loss stop", observed=stop, unit=EvidenceUnit.PRICE
            ),
            ExplanationFactV1(
                label="Maximum loss", observed=loss, unit=EvidenceUnit.PERCENT
            ),
        ]
        if stop <= 0:
            return StopLevelV1(
                rule_code="no_level", summary="No positive stop level.", facts=facts
            )
        return StopLevelV1(
            level=stop,
            rule_code="maximum_loss_stop",
            summary=f"Max loss {loss.normalize():f}% from average cost",
            facts=facts,
            basis="average_cost",
            trigger="close_lte",
        )

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int | Decimal:
        """SELL the held quantity; BUY sizing is engine-owned (returns 0)."""
        if signal.side == SignalSide.SELL:
            held = _held(portfolio, signal.security_id)
            return Decimal(0) if held is None else held.quantity
        return 0


strategy = FiftyTwoWeekHighStrategy()
