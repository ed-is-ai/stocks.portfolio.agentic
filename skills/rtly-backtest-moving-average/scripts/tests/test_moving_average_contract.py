"""Contract tests for the moving-average backtest Strategy."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pandas as pd

from app.services.backtest.strategy_protocol import (
    PortfolioView,
    PositionSummaryV1,
    Signal,
    SignalSide,
    StrategyProtocolV1,
    validate_entry_signals,
    validate_exit_signals,
    validate_position_size,
)

_RUNTIME = Path(__file__).resolve().parents[1] / "strategy.py"
_SPEC = spec_from_file_location("moving_average_backtest_strategy", _RUNTIME)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
MovingAverageStrategy = _MODULE.MovingAverageStrategy

AS_OF = date(2026, 1, 8)
PARAMETERS = {
    "security_id": "sec-aapl",
    "fixed_shares": 10,
    "fast_window": 2,
    "slow_window": 3,
}


class _View:
    def __init__(self, closes: list[object], *, latest: date = AS_OF) -> None:
        self.as_of_session = AS_OF
        start = latest - timedelta(days=len(closes) - 1)
        index = [start + timedelta(days=offset) for offset in range(len(closes))]
        self._history = pd.DataFrame({"close": closes}, index=index)

    def price_history(self, security_id: str) -> pd.DataFrame:
        return self._history.copy()

    def scan_result(self, security_id: str) -> None:
        return None


def _portfolio(quantity: str | None = None) -> PortfolioView:
    positions = ()
    if quantity is not None:
        positions = (
            PositionSummaryV1(
                security_id="sec-aapl",
                quantity=Decimal(quantity),
                average_cost=Decimal("100"),
            ),
        )
    return PortfolioView(
        as_of_session=AS_OF,
        base_currency="GBP",
        cash=Decimal("1000"),
        positions=positions,
        volatility_observations=(),
    )


def test_protocol_identity_and_deterministic_bullish_crossover() -> None:
    strategy = MovingAverageStrategy()
    view = _View([Decimal("3"), Decimal("2"), Decimal("1"), Decimal("4")])

    first = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))
    second = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))

    assert isinstance(strategy, StrategyProtocolV1)
    assert _MODULE.STRATEGY_ID == "rtly-backtest-moving-average"
    assert _MODULE.STRATEGY_API_VERSION == 1
    assert first == second
    assert [(signal.side, signal.session) for signal in first] == [
        (SignalSide.BUY, AS_OF)
    ]


def test_bearish_crossover_exits_only_a_held_target() -> None:
    strategy = MovingAverageStrategy()
    view = _View([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("0")])

    held = validate_exit_signals(
        strategy.exit_signals(view, _portfolio("7"), PARAMETERS)
    )
    unheld = validate_exit_signals(
        strategy.exit_signals(view, _portfolio(), PARAMETERS)
    )
    zero_quantity = validate_exit_signals(
        strategy.exit_signals(view, _portfolio("0"), PARAMETERS)
    )

    assert [signal.side for signal in held] == [SignalSide.SELL]
    assert unheld == ()
    assert zero_quantity == ()


def test_requires_slow_window_plus_one_and_rejects_equal_or_inverted_windows() -> None:
    strategy = MovingAverageStrategy()
    assert strategy.entry_signals(_View([1, 2, 3]), PARAMETERS) == []
    assert strategy.entry_signals(_View([2, 2, 2, 2]), PARAMETERS) == []
    assert (
        strategy.entry_signals(_View([3, 2, 1, 4]), {**PARAMETERS, "fast_window": 3})
        == []
    )


def test_missing_malformed_and_stale_history_fail_closed() -> None:
    strategy = MovingAverageStrategy()
    stale = _View([3, 2, 1, 4], latest=AS_OF - timedelta(days=1))
    malformed = _View([3, 2, "not-a-price", 4])
    missing = _View([3, 2, 1, 4])
    missing._history = pd.DataFrame(
        {"open": [3, 2, 1, 4]}, index=missing._history.index
    )

    assert strategy.entry_signals(stale, PARAMETERS) == []
    assert strategy.entry_signals(malformed, PARAMETERS) == []
    assert strategy.entry_signals(missing, PARAMETERS) == []


def test_position_size_uses_fixed_buy_and_full_integral_sell() -> None:
    strategy = MovingAverageStrategy()
    view = _View([3, 2, 1, 4])
    buy = strategy.entry_signals(view, {**PARAMETERS, "fixed_shares": 12})[0]
    sell = Signal(
        security_id="sec-aapl",
        side=SignalSide.SELL,
        session=AS_OF,
        rule_id="test_sell",
    )

    assert (
        validate_position_size(
            strategy.position_size(
                buy, view, _portfolio(), {**PARAMETERS, "fixed_shares": 12}
            )
        )
        == 12
    )
    assert strategy.position_size(sell, view, _portfolio("7"), PARAMETERS) == 7
    assert strategy.position_size(sell, view, _portfolio("7.5"), PARAMETERS) == 0
    assert strategy.position_size(sell, view, _portfolio(), PARAMETERS) == 0
