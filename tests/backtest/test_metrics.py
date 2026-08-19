"""Story 2.5 coverage: the full I/O/edge-case matrix for ``metrics.py`` --
AD-8's Total Return/Sharpe/Win Rate/Max Drawdown formulas, their null
rules, and the typed availability-reason projection."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.backtest.backtest_engine import EquityCurvePointV1, ExitFillEventV1
from app.services.backtest.metrics import (
    BacktestMetricsV1,
    MetricUnavailableReason,
    MetricsError,
    calculate_metrics,
    metric_availability,
)

STARTING_CAPITAL = Decimal("10000")


def _point(
    session: date, equity: str, *, cash: str | None = None, seq: int
) -> EquityCurvePointV1:
    cash_value = equity if cash is None else cash
    return EquityCurvePointV1(
        session=session,
        cash_base=Decimal(cash_value),
        positions_value_base=Decimal(equity) - Decimal(cash_value),
        total_equity_base=Decimal(equity),
        sequence=seq,
    )


def _exit(pnl: str, *, seq: int, security_id: str = "AAA") -> ExitFillEventV1:
    return ExitFillEventV1(
        security_id=security_id,
        signal_session=date(2026, 1, 1),
        fill_session=date(2026, 1, 2),
        rule_id="rule-1",
        shares=10,
        fill_price_native=Decimal("100"),
        fill_currency="USD",
        fill_quote_unit="USD",
        proceeds_base=Decimal("1000") + Decimal(pnl),
        cost_basis_base=Decimal("1000"),
        realized_pnl_base=Decimal(pnl),
        sequence=seq,
    )


def _curve(*equities: str) -> tuple[EquityCurvePointV1, ...]:
    return tuple(
        _point(date(2026, 1, 1 + index), equity, seq=index + 1)
        for index, equity in enumerate(equities)
    )


# ---------------------------------------------------------------------------
# Total Return / Max Drawdown
# ---------------------------------------------------------------------------


def test_total_return_is_ending_over_starting_minus_one() -> None:
    curve = _curve("10000", "10500", "11000")

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=()
    )

    assert metrics.total_return == pytest.approx(0.10)


def test_total_return_can_be_negative() -> None:
    curve = _curve("10000", "9500", "9000")

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=()
    )

    assert metrics.total_return == pytest.approx(-0.10)


def test_never_declining_curve_has_zero_drawdown_never_positive() -> None:
    curve = _curve("10000", "10100", "10100", "10500")

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=()
    )

    assert metrics.max_drawdown == 0.0


def test_drawdown_and_recovery_reports_the_deepest_trough() -> None:
    curve = _curve("10000", "10000", "8000", "9000", "10500")

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=()
    )

    assert metrics.max_drawdown is not None
    assert metrics.max_drawdown == pytest.approx(-0.20)
    assert metrics.max_drawdown <= 0.0


def test_final_open_position_marks_feed_total_return_and_drawdown_only() -> None:
    """An ``OpenPositionMarkEventV1`` is folded into the final Equity Curve
    point's ``total_equity_base`` by the engine (Story 2.4) -- it is never
    itself a closed trade, so it must never enter Win Rate."""
    curve = _curve("10000", "10800")  # final point already reflects the mark

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=()
    )

    assert metrics.total_return == pytest.approx(0.08)
    assert metrics.win_rate is None


# ---------------------------------------------------------------------------
# Win Rate
# ---------------------------------------------------------------------------


def test_zero_closed_trades_yields_null_win_rate() -> None:
    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL,
        equity_curve=_curve("10000", "10500"),
        closed_trades=(),
    )

    assert metrics.win_rate is None


def test_break_even_trade_counts_in_denominator_not_numerator() -> None:
    trades = (_exit("0", seq=1),)

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL,
        equity_curve=_curve("10000", "10000"),
        closed_trades=trades,
    )

    assert metrics.win_rate == 0.0


def test_win_rate_is_profitable_over_all_closed_trades() -> None:
    trades = (
        _exit("100", seq=1),
        _exit("-50", seq=2),
        _exit("0", seq=3),
        _exit("25", seq=4),
    )

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL,
        equity_curve=_curve("10000", "10075"),
        closed_trades=trades,
    )

    assert metrics.win_rate == pytest.approx(2 / 4)


# ---------------------------------------------------------------------------
# Sharpe
# ---------------------------------------------------------------------------


def test_one_daily_return_yields_null_sharpe() -> None:
    curve = _curve("10000", "10500")

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=()
    )

    assert metrics.sharpe_ratio is None


def test_zero_variance_daily_returns_yields_null_sharpe_never_divides_by_zero() -> None:
    curve = _curve("10000", "10100", "10201", "10303.01")  # constant 1% daily return

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=()
    )

    assert metrics.sharpe_ratio is None


def test_sharpe_uses_sample_stdev_ddof1_and_sqrt_252_annualization() -> None:
    curve = _curve("10000", "10100", "9900", "10300")

    metrics = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=()
    )

    # Daily returns: 0.01, -0.0198019..., 0.0404040...
    returns = [0.01, (9900 - 10100) / 10100, (10300 - 9900) / 9900]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    expected = mean / (variance**0.5) * (252**0.5)

    assert metrics.sharpe_ratio == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# Determinism / repeatability
# ---------------------------------------------------------------------------


def test_metrics_are_deterministic_across_repeated_calls() -> None:
    curve = _curve("10000", "10250", "9800", "10600")
    trades = (_exit("150", seq=1), _exit("-40", seq=2))

    first = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=trades
    )
    second = calculate_metrics(
        starting_capital=STARTING_CAPITAL, equity_curve=curve, closed_trades=trades
    )

    assert first == second
    assert isinstance(first, BacktestMetricsV1)


# ---------------------------------------------------------------------------
# Availability reasons (AC 5) -- never persisted, only surfaced on retrieval
# ---------------------------------------------------------------------------


def test_metric_availability_reports_no_closed_trades() -> None:
    availability = metric_availability(
        equity_curve=_curve("10000", "10500", "10800"), closed_trades=()
    )

    assert availability.win_rate_unavailable == MetricUnavailableReason.NO_CLOSED_TRADES
    assert availability.sharpe_unavailable is None


def test_metric_availability_reports_insufficient_daily_returns() -> None:
    availability = metric_availability(
        equity_curve=_curve("10000", "10500"), closed_trades=(_exit("10", seq=1),)
    )

    assert availability.win_rate_unavailable is None
    assert (
        availability.sharpe_unavailable
        == MetricUnavailableReason.INSUFFICIENT_DAILY_RETURNS
    )


def test_metric_availability_reports_zero_variance() -> None:
    curve = _curve("10000", "10100", "10201", "10303.01")

    availability = metric_availability(equity_curve=curve, closed_trades=())

    assert availability.sharpe_unavailable == MetricUnavailableReason.ZERO_VARIANCE


def test_metric_availability_reports_nothing_unavailable_when_computable() -> None:
    curve = _curve("10000", "10100", "9900", "10300")
    trades = (_exit("50", seq=1),)

    availability = metric_availability(equity_curve=curve, closed_trades=trades)

    assert availability.win_rate_unavailable is None
    assert availability.sharpe_unavailable is None


# ---------------------------------------------------------------------------
# Structural validation -- stable integrity errors, never silent corruption
# ---------------------------------------------------------------------------


def test_non_positive_starting_capital_is_rejected() -> None:
    with pytest.raises(MetricsError) as excinfo:
        calculate_metrics(
            starting_capital=Decimal("0"),
            equity_curve=_curve("10000", "10500"),
            closed_trades=(),
        )
    assert excinfo.value.code == "invalid_starting_capital"


def test_empty_equity_curve_is_rejected() -> None:
    with pytest.raises(MetricsError) as excinfo:
        calculate_metrics(
            starting_capital=STARTING_CAPITAL, equity_curve=(), closed_trades=()
        )
    assert excinfo.value.code == "empty_equity_curve"


def test_unordered_equity_curve_is_rejected() -> None:
    out_of_order = (
        _point(date(2026, 1, 2), "10500", seq=1),
        _point(date(2026, 1, 1), "10000", seq=2),
    )
    with pytest.raises(MetricsError) as excinfo:
        calculate_metrics(
            starting_capital=STARTING_CAPITAL,
            equity_curve=out_of_order,
            closed_trades=(),
        )
    assert excinfo.value.code == "unordered_equity_curve"


def test_duplicate_session_equity_curve_is_rejected() -> None:
    duplicate = (
        _point(date(2026, 1, 1), "10000", seq=1),
        _point(date(2026, 1, 1), "10500", seq=2),
    )
    with pytest.raises(MetricsError) as excinfo:
        calculate_metrics(
            starting_capital=STARTING_CAPITAL, equity_curve=duplicate, closed_trades=()
        )
    assert excinfo.value.code == "unordered_equity_curve"


def test_non_finite_equity_is_rejected() -> None:
    finite_point = _point(date(2026, 1, 1), "10000", seq=1)
    non_finite_point = EquityCurvePointV1.model_construct(
        session=date(2026, 1, 2),
        cash_base=Decimal("Infinity"),
        positions_value_base=Decimal("0"),
        total_equity_base=Decimal("Infinity"),
        sequence=2,
    )
    with pytest.raises(MetricsError) as excinfo:
        calculate_metrics(
            starting_capital=STARTING_CAPITAL,
            equity_curve=(finite_point, non_finite_point),
            closed_trades=(),
        )
    assert excinfo.value.code == "invalid_equity"
