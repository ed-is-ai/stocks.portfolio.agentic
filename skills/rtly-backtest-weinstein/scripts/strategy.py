"""Deterministic long-only Weinstein Stage 2 breakout backtest Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-weinstein"
STRATEGY_API_VERSION = 1
_ENTRY_RULE = "weinstein_stage2_breakout_v1"
_EXIT_RULE = "weinstein_stage_exit_v1"


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


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _decimals(values: Any) -> list[Decimal] | None:
    result = [_decimal(value) for value in values]
    return None if any(value is None for value in result) else list(result)  # type: ignore[arg-type]


def _plain_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _session_date(value: Any) -> date | None:
    if type(value) is date:
        return value
    converter = getattr(value, "date", None)
    if callable(converter):
        converted = converter()
        return converted if isinstance(converted, date) else None
    return None


def _current_history(view: MarketViewV1, security_id: str) -> Any | None:
    history = view.price_history(security_id)
    if history.empty or _session_date(history.index[-1]) != view.as_of_session:
        return None
    if not {"high", "close", "volume"}.issubset(history.columns):
        return None
    return history


def _visible_scan(view: MarketViewV1, security_id: str) -> Any | None:
    scan = view.scan_result(security_id)
    if scan is None or getattr(scan, "security_id", None) != security_id:
        return None
    as_of = getattr(scan, "as_of_session_date", None)
    if not isinstance(as_of, date) or as_of > view.as_of_session:
        return None
    return scan


def _position(portfolio: PortfolioView, security_id: str) -> Any | None:
    return next(
        (item for item in portfolio.positions if item.security_id == security_id),
        None,
    )


def _integral_quantity(portfolio: PortfolioView, security_id: str) -> int:
    held = _position(portfolio, security_id)
    if held is None or held.quantity <= 0:
        return 0
    integral = held.quantity.to_integral_value()
    return int(integral) if held.quantity == integral else 0


def _sma_slope(prices: list[Decimal], window: int, lookback: int = 4) -> Decimal | None:
    if len(prices) < window + lookback:
        return None
    current = sum(prices[-window:], Decimal(0)) / Decimal(window)
    past = sum(prices[-(window + lookback) : -lookback], Decimal(0)) / Decimal(window)
    return current - past


def _weekly_closes(history: Any) -> list[Decimal] | None:
    grouped: dict[tuple[int, int], Decimal] = {}
    try:
        sessions = history.index
        closes = history["close"]
    except (AttributeError, KeyError, TypeError):
        return None
    for session, raw_close in zip(sessions, closes, strict=True):
        session_date = _session_date(session)
        close = _decimal(raw_close)
        if session_date is None or close is None:
            return None
        iso = session_date.isocalendar()
        grouped[(iso.year, iso.week)] = close
    return [grouped[key] for key in sorted(grouped)][-52:]


def _classify_stage(
    *,
    price: Decimal,
    sma150: Decimal,
    sma200: Decimal,
    weekly: list[Decimal],
) -> str | None:
    slope150 = _sma_slope(weekly, window=30)
    slope200 = _sma_slope(weekly, window=40)
    if slope150 is None or slope200 is None:
        return None
    above_150 = price > sma150
    above_200 = price > sma200
    ma_bullish = sma150 > sma200
    if above_150 and ma_bullish and slope200 > 0:
        return "Stage 2"
    if not above_150 and not above_200 and slope150 < 0:
        return "Stage 4"
    if (not above_150 and above_200) or (above_150 and ma_bullish and slope200 <= 0):
        return "Stage 3"
    return "Stage 1"


class WeinsteinStrategy:
    """Apply Stage 2 breakout and Stage/risk exit rules without state."""

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        signals = [
            self._entry_signal(view, parameters, security_id)
            for security_id in _universe(parameters)
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
        history = _current_history(view, security_id)
        scan = _visible_scan(view, security_id)
        lookback = _plain_int(parameters["breakout_lookback_sessions"])
        if lookback is None:
            return None
        required = max(204, lookback + 1, 51)
        if history is None or scan is None or len(history) < required:
            return None

        closes = _decimals(history["close"])
        highs = _decimals(history["high"])
        volumes = _decimals(history["volume"])
        weekly = _weekly_closes(history)
        if closes is None or highs is None or volumes is None or weekly is None:
            return None
        close = closes[-1]
        sma150 = sum(closes[-150:], Decimal(0)) / Decimal(150)
        sma200 = sum(closes[-200:], Decimal(0)) / Decimal(200)
        daily_stage = _classify_stage(
            price=close,
            sma150=sma150,
            sma200=sma200,
            weekly=weekly,
        )
        prior_high = max(highs[-(lookback + 1) : -1])
        prior_volume_mean = sum(volumes[-51:-1], Decimal(0)) / Decimal(50)
        minimum_volume = _decimal(parameters["minimum_relative_volume"])
        scan_stage = getattr(getattr(scan, "stage", None), "value", None)
        if (
            minimum_volume is None
            or prior_volume_mean <= 0
            or volumes[-1] < 0
            or scan_stage != "Stage 2"
            or daily_stage != "Stage 2"
            or close <= prior_high
            or volumes[-1] < prior_volume_mean * minimum_volume
        ):
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=view.as_of_session,
            rule_id=_ENTRY_RULE,
        )

    def _exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> Signal | None:
        held = _position(portfolio, security_id)
        history = _current_history(view, security_id)
        scan = _visible_scan(view, security_id)
        if held is None or held.quantity <= 0 or history is None or len(history) < 150:
            return None
        closes = _decimals(history["close"].iloc[-150:])
        maximum_loss = _decimal(parameters["maximum_loss_pct"])
        if closes is None or maximum_loss is None:
            return None
        close = closes[-1]
        sma150 = sum(closes, Decimal(0)) / Decimal(150)
        stop = held.average_cost * (Decimal(1) - maximum_loss / Decimal(100))
        scan_stage = getattr(getattr(scan, "stage", None), "value", None)
        scan_failure = scan is not None and scan_stage != "Stage 2"
        if not (close <= stop or close < sma150 or scan_failure):
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id=_EXIT_RULE,
        )

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        if signal.side == SignalSide.SELL:
            return _integral_quantity(portfolio, signal.security_id)
        fixed_shares = parameters["fixed_shares"]
        if isinstance(fixed_shares, int) and not isinstance(fixed_shares, bool):
            return fixed_shares
        return 0
