"""Contract and boundary tests for the Turtle Trend backtest Strategy."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

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


def _load_strategy_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "strategy.py"
    spec = spec_from_file_location("turtle_trend_backtest_strategy", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_strategy_module()


class _View:
    def __init__(self, as_of_session: date, history: pd.DataFrame) -> None:
        self.as_of_session = as_of_session
        self._history = history

    def price_history(self, security_id: str) -> pd.DataFrame:
        return self._history.copy()

    def scan_result(self, security_id: str):  # noqa: ANN201
        return None


def _history(
    *, current_high: str = "13", current_low: str = "7", stale: bool = False
) -> tuple[date, pd.DataFrame]:
    as_of = date(2026, 1, 8)
    last = as_of - timedelta(days=1) if stale else as_of
    sessions = [last - timedelta(days=3 - offset) for offset in range(4)]
    return as_of, pd.DataFrame(
        {
            "open": [Decimal("10")] * 4,
            "high": [
                Decimal("10"),
                Decimal("11"),
                Decimal("12"),
                Decimal(current_high),
            ],
            "low": [Decimal("10"), Decimal("9"), Decimal("8"), Decimal(current_low)],
            "close": [Decimal("10")] * 4,
            "volume": [Decimal("100")] * 4,
        },
        index=sessions,
    )


def _parameters() -> dict[str, object]:
    return {
        "selected_securities": ["sec-aapl"],
        "fixed_shares": 10,
        "entry_lookback_sessions": 3,
        "exit_lookback_sessions": 2,
    }


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
        as_of_session=date(2026, 1, 8),
        base_currency="GBP",
        cash=Decimal("1000"),
        positions=positions,
        volatility_observations=(),
    )


def test_identity_protocol_and_deterministic_entry() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, history = _history()
    view = _View(as_of, history)

    first = validate_entry_signals(strategy.entry_signals(view, _parameters()))
    second = validate_entry_signals(strategy.entry_signals(view, _parameters()))

    assert MODULE.STRATEGY_ID == "rtly-backtest-turtle-trend"
    assert MODULE.STRATEGY_API_VERSION == 1
    assert isinstance(strategy, StrategyProtocolV1)
    assert first == second
    assert [(signal.side, signal.rule_id) for signal in first] == [
        (SignalSide.BUY, "turtle_entry_channel_breakout_v1")
    ]


def test_entry_channel_is_strict_and_excludes_current_bar() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, equality = _history(current_high="12")
    _, breakout = _history(current_high="12.01")

    assert strategy.entry_signals(_View(as_of, equality), _parameters()) == []
    assert len(strategy.entry_signals(_View(as_of, breakout), _parameters())) == 1


def test_exit_uses_its_own_warmup_and_strict_low_channel() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, equality = _history(current_low="8")
    _, breach = _history(current_low="7.99")
    portfolio = _portfolio("7")

    assert strategy.exit_signals(_View(as_of, equality), portfolio, _parameters()) == []
    exits = validate_exit_signals(
        strategy.exit_signals(_View(as_of, breach), portfolio, _parameters())
    )
    assert [(signal.side, signal.rule_id) for signal in exits] == [
        (SignalSide.SELL, "turtle_exit_channel_breach_v1")
    ]

    short = breach.iloc[-3:]
    assert strategy.entry_signals(_View(as_of, short), _parameters()) == []
    assert (
        len(strategy.exit_signals(_View(as_of, short), portfolio, _parameters())) == 1
    )


def test_empty_stale_and_non_finite_history_fail_closed() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, stale = _history(stale=True)
    _, history = _history()
    _, malformed = _history(current_high="Infinity")
    empty = history.iloc[0:0]

    assert strategy.entry_signals(_View(as_of, stale), _parameters()) == []
    assert strategy.entry_signals(_View(as_of, malformed), _parameters()) == []
    assert strategy.entry_signals(_View(as_of, empty), _parameters()) == []


def test_unheld_security_does_not_exit() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, history = _history()
    assert (
        strategy.exit_signals(_View(as_of, history), _portfolio(), _parameters()) == []
    )


def test_position_size_uses_fixed_buy_and_full_integral_sell_quantity() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, history = _history()
    view = _View(as_of, history)
    buy = Signal(
        security_id="sec-aapl",
        side=SignalSide.BUY,
        session=as_of,
        rule_id="buy",
    )
    sell = Signal(
        security_id="sec-aapl",
        side=SignalSide.SELL,
        session=as_of,
        rule_id="sell",
    )

    assert (
        validate_position_size(
            strategy.position_size(buy, view, _portfolio(), _parameters())
        )
        == 10
    )
    assert (
        validate_position_size(
            strategy.position_size(sell, view, _portfolio("7"), _parameters())
        )
        == 7
    )
    assert strategy.position_size(sell, view, _portfolio("7.5"), _parameters()) == 0


def test_multi_security_universe_breaks_out_per_selected_security() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, history = _history()
    view = _View(as_of, history)

    signals = validate_entry_signals(
        strategy.entry_signals(
            view,
            {
                **_parameters(),
                "selected_securities": ["sec-msft", "sec-aapl", "sec-msft"],
            },
        )
    )

    assert [signal.security_id for signal in signals] == ["sec-aapl", "sec-msft"]


def test_multi_security_universe_exits_only_held_securities() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, breach = _history(current_low="7.99")

    exits = validate_exit_signals(
        strategy.exit_signals(
            _View(as_of, breach),
            _portfolio("7"),
            {**_parameters(), "selected_securities": ["sec-msft", "sec-aapl"]},
        )
    )

    assert [signal.security_id for signal in exits] == ["sec-aapl"]


def test_empty_or_malformed_universe_emits_nothing() -> None:
    strategy = MODULE.TurtleTrendStrategy()
    as_of, history = _history()
    view = _View(as_of, history)
    without_universe = {
        name: value
        for name, value in _parameters().items()
        if name != "selected_securities"
    }

    assert (
        strategy.entry_signals(view, {**_parameters(), "selected_securities": []}) == []
    )
    assert (
        strategy.entry_signals(
            view, {**_parameters(), "selected_securities": "sec-aapl"}
        )
        == []
    )
    assert strategy.entry_signals(view, without_universe) == []
