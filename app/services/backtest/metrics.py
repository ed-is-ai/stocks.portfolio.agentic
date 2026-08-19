"""Sole Metrics authority for a completed Backtest Result (AD-8, Story 2.5).

``calculate_metrics`` is the one place Total Return, Sharpe, Win Rate, and
Max Drawdown are computed from a completed simulation's daily Equity Curve
and closed trades (:class:`ExitFillEventV1` -- the only Trade Log event
carrying a realized P&L). It never recomputes fills, P&L, corporate
actions, or FX; those are Story 2.4's ``backtest_engine.py`` output,
consumed here exactly as given.

Two null-producing conditions exist for Sharpe/Win Rate, and
:func:`metric_availability` exposes *why* a null resulted (AC 5) without
adding a fifth key to the persisted, fixed four-key ``metrics_json``
(AD-8) -- that typed reason lives only in a retrieval-side projection a
repository builds by calling this module, never in storage.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException
from enum import StrEnum
import statistics

from pydantic import BaseModel, ConfigDict

from app.services.backtest.backtest_engine import EquityCurvePointV1, ExitFillEventV1
from app.services.backtest.market_planes import deterministic_decimal_context

#: Trading sessions per year used to annualize Sharpe at a 0% risk-free
#: rate (AD-8).
_TRADING_SESSIONS_PER_YEAR = 252


class MetricsError(ValueError):
    """A stable, machine-readable failure validating Metrics inputs.

    Reserved for structurally invalid input (non-positive starting
    capital, an empty/unordered/non-finite Equity Curve, a non-finite
    closed-trade P&L, or arithmetic that cannot be represented) -- never
    raised for the documented null-Metric cases (zero closed trades,
    fewer than two daily returns, zero return variance).
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _MetricsModel(BaseModel):
    """Frozen, strict, extra-forbidding base matching this codebase's
    established immutable-model convention."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class BacktestMetricsV1(_MetricsModel):
    """AD-8's fixed four-key Metrics shape.

    Every ratio is an unrounded finite float, or ``null`` exactly per the
    documented availability rules -- never a bare ``0`` standing in for
    "not applicable".
    """

    total_return: float | None
    sharpe_ratio: float | None
    win_rate: float | None
    max_drawdown: float | None


class MetricUnavailableReason(StrEnum):
    """Stable, machine-readable reasons Win Rate/Sharpe is ``null``."""

    NO_CLOSED_TRADES = "no_closed_trades"
    INSUFFICIENT_DAILY_RETURNS = "insufficient_daily_returns"
    ZERO_VARIANCE = "zero_variance"


class MetricAvailabilityV1(_MetricsModel):
    """Typed reasons a retrieval projection surfaces for null Metrics
    (AC 5) -- computed by this module but never persisted as an extra
    ``metrics_json`` key."""

    win_rate_unavailable: MetricUnavailableReason | None = None
    sharpe_unavailable: MetricUnavailableReason | None = None


def _validate_starting_capital(starting_capital: Decimal) -> None:
    if not starting_capital.is_finite() or starting_capital <= 0:
        raise MetricsError(
            "invalid_starting_capital",
            "starting capital must be positive and finite",
        )


def _validate_equity_curve(equity_curve: tuple[EquityCurvePointV1, ...]) -> None:
    if not equity_curve:
        raise MetricsError("empty_equity_curve", "equity curve must not be empty")
    previous_session = None
    for point in equity_curve:
        if not point.total_equity_base.is_finite():
            raise MetricsError(
                "invalid_equity", "equity curve total_equity_base must be finite"
            )
        if previous_session is not None and point.session <= previous_session:
            raise MetricsError(
                "unordered_equity_curve",
                "equity curve sessions must be strictly ascending",
            )
        previous_session = point.session


def _validate_closed_trades(closed_trades: tuple[ExitFillEventV1, ...]) -> None:
    for trade in closed_trades:
        if not trade.realized_pnl_base.is_finite():
            raise MetricsError(
                "invalid_closed_trade", "realized_pnl_base must be finite"
            )


def _total_return(
    starting_capital: Decimal, equity_curve: tuple[EquityCurvePointV1, ...]
) -> float:
    ending_equity = equity_curve[-1].total_equity_base
    try:
        with deterministic_decimal_context():
            ratio = (ending_equity - starting_capital) / starting_capital
        return float(ratio)
    except (DecimalException, OverflowError) as exc:
        raise MetricsError("integrity_error", "total return arithmetic failed") from exc


def _max_drawdown(equity_curve: tuple[EquityCurvePointV1, ...]) -> float:
    try:
        with deterministic_decimal_context():
            peak = equity_curve[0].total_equity_base
            min_drawdown = Decimal(0)
            for point in equity_curve:
                peak = max(peak, point.total_equity_base)
                drawdown = point.total_equity_base / peak - 1
                min_drawdown = min(min_drawdown, drawdown)
        return float(min_drawdown)
    except (DecimalException, OverflowError) as exc:
        raise MetricsError("integrity_error", "max drawdown arithmetic failed") from exc


def _daily_returns(equity_curve: tuple[EquityCurvePointV1, ...]) -> list[Decimal]:
    """Return ``equity[D] / equity[previous_D] - 1`` for every consecutive
    pair on the curve -- the exact series Sharpe is computed from."""
    try:
        with deterministic_decimal_context():
            returns: list[Decimal] = []
            for previous, current in zip(equity_curve, equity_curve[1:]):
                if previous.total_equity_base == 0:
                    raise MetricsError(
                        "integrity_error",
                        "daily return arithmetic divides by zero equity",
                    )
                returns.append(
                    current.total_equity_base / previous.total_equity_base - 1
                )
    except DecimalException as exc:
        raise MetricsError("integrity_error", "daily return arithmetic failed") from exc
    return returns


def _sample_stdev(daily_returns: list[Decimal]) -> Decimal | None:
    """Return the sample stdev (``ddof=1``) of ``daily_returns``, or
    ``None`` when fewer than two returns exist to compute one from."""
    if len(daily_returns) < 2:
        return None
    try:
        with deterministic_decimal_context():
            return statistics.stdev(daily_returns)
    except (DecimalException, statistics.StatisticsError) as exc:
        raise MetricsError("integrity_error", "sharpe ratio arithmetic failed") from exc


def _sharpe_ratio(daily_returns: list[Decimal]) -> float | None:
    sample_stdev = _sample_stdev(daily_returns)
    if sample_stdev is None or sample_stdev == 0:
        return None
    try:
        with deterministic_decimal_context():
            mean_return = statistics.mean(daily_returns)
            annualization = Decimal(_TRADING_SESSIONS_PER_YEAR).sqrt()
            ratio = (mean_return / sample_stdev) * annualization
        return float(ratio)
    except (DecimalException, statistics.StatisticsError, OverflowError) as exc:
        raise MetricsError("integrity_error", "sharpe ratio arithmetic failed") from exc


def _win_rate(closed_trades: tuple[ExitFillEventV1, ...]) -> float | None:
    if not closed_trades:
        return None
    wins = sum(1 for trade in closed_trades if trade.realized_pnl_base > 0)
    return wins / len(closed_trades)


def calculate_metrics(
    *,
    starting_capital: Decimal,
    equity_curve: tuple[EquityCurvePointV1, ...],
    closed_trades: tuple[ExitFillEventV1, ...],
) -> BacktestMetricsV1:
    """Compute AD-8's four Metrics from a completed simulation's exact
    Equity Curve and closed trades.

    Pure: never recomputes fills/P&L/actions/FX (Story 2.4's authority),
    never touches a repository or the filesystem. ``closed_trades`` must
    contain only :class:`ExitFillEventV1` events -- skips, corporate
    actions, and :class:`OpenPositionMarkEventV1` final marks are never
    closed trades and must be filtered out by the caller before this call.
    Raises :class:`MetricsError` for structurally invalid input; never for
    the documented null cases (those return ``None`` for that key).
    """
    _validate_starting_capital(starting_capital)
    _validate_equity_curve(equity_curve)
    _validate_closed_trades(closed_trades)
    daily_returns = _daily_returns(equity_curve)
    return BacktestMetricsV1(
        total_return=_total_return(starting_capital, equity_curve),
        sharpe_ratio=_sharpe_ratio(daily_returns),
        win_rate=_win_rate(closed_trades),
        max_drawdown=_max_drawdown(equity_curve),
    )


def metric_availability(
    *,
    equity_curve: tuple[EquityCurvePointV1, ...],
    closed_trades: tuple[ExitFillEventV1, ...],
) -> MetricAvailabilityV1:
    """Return typed reasons Win Rate/Sharpe are ``null`` for retrieval
    (AC 5), computed with the exact same rules :func:`calculate_metrics`
    uses -- never persisted as extra ``metrics_json`` keys."""
    win_rate_unavailable = (
        None if closed_trades else MetricUnavailableReason.NO_CLOSED_TRADES
    )
    daily_returns = _daily_returns(equity_curve)
    if len(daily_returns) < 2:
        sharpe_unavailable: MetricUnavailableReason | None = (
            MetricUnavailableReason.INSUFFICIENT_DAILY_RETURNS
        )
    else:
        sample_stdev = _sample_stdev(daily_returns)
        sharpe_unavailable = (
            MetricUnavailableReason.ZERO_VARIANCE if sample_stdev == 0 else None
        )
    return MetricAvailabilityV1(
        win_rate_unavailable=win_rate_unavailable,
        sharpe_unavailable=sharpe_unavailable,
    )


__all__ = [
    "BacktestMetricsV1",
    "MetricAvailabilityV1",
    "MetricUnavailableReason",
    "MetricsError",
    "calculate_metrics",
    "metric_availability",
]
