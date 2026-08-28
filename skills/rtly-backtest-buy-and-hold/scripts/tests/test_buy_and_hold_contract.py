"""Contract tests for the buy-and-hold backtest Strategy."""

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
)

_RUNTIME = Path(__file__).resolve().parents[1] / "strategy.py"
_SPEC = spec_from_file_location("buy_and_hold_backtest_strategy", _RUNTIME)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
BuyAndHoldStrategy = _MODULE.BuyAndHoldStrategy

AS_OF = date(2026, 1, 8)
PARAMETERS = {
    "selected_securities": ["sec-aapl"],
    "entry_on_or_after": "2000-01-01",
}


class _View:
    def __init__(
        self,
        *,
        latest: date = AS_OF,
        empty: bool = False,
        current_close: str = "100",
    ) -> None:
        self.as_of_session = AS_OF
        index = [] if empty else [latest]
        values = [] if empty else [Decimal("100")]
        closes = [] if empty else [Decimal(current_close)]
        self._history = pd.DataFrame(
            {
                "open": values,
                "high": values,
                "low": values,
                "close": closes,
                "volume": values,
            },
            index=index,
        )

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


def test_protocol_identity_and_repeat_deterministic_candidates() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()

    first = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))
    second = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))

    assert isinstance(strategy, StrategyProtocolV1)
    assert _MODULE.STRATEGY_ID == "rtly-backtest-buy-and-hold"
    assert _MODULE.STRATEGY_API_VERSION == 1
    assert first == second
    assert [(signal.side, signal.session) for signal in first] == [
        (SignalSide.BUY, AS_OF)
    ]


def test_cutoff_is_inclusive_and_invalid_or_future_cutoffs_fail_closed() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()

    assert (
        len(
            strategy.entry_signals(
                view, {**PARAMETERS, "entry_on_or_after": AS_OF.isoformat()}
            )
        )
        == 1
    )
    assert (
        strategy.entry_signals(
            view,
            {
                **PARAMETERS,
                "entry_on_or_after": (AS_OF + timedelta(days=1)).isoformat(),
            },
        )
        == []
    )
    assert (
        strategy.entry_signals(view, {**PARAMETERS, "entry_on_or_after": "not-a-date"})
        == []
    )


def test_missing_and_stale_target_history_fail_closed() -> None:
    strategy = BuyAndHoldStrategy()

    assert strategy.entry_signals(_View(empty=True), PARAMETERS) == []
    assert (
        strategy.entry_signals(_View(latest=AS_OF - timedelta(days=1)), PARAMETERS)
        == []
    )
    assert strategy.entry_signals(_View(current_close="NaN"), PARAMETERS) == []


def test_nullable_unused_volume_does_not_block_passive_entry() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()
    view._history.loc[AS_OF, "volume"] = None

    assert len(strategy.entry_signals(view, PARAMETERS)) == 1


def test_strategy_never_exits() -> None:
    strategy = BuyAndHoldStrategy()
    assert (
        validate_exit_signals(
            strategy.exit_signals(_View(), _portfolio("10"), PARAMETERS)
        )
        == ()
    )


def test_position_size_defers_buy_to_engine_and_sizes_sell_integrally() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()
    buy = strategy.entry_signals(view, PARAMETERS)[0]
    sell = Signal(
        security_id="sec-aapl",
        side=SignalSide.SELL,
        session=AS_OF,
        rule_id="test_sell",
    )

    assert strategy.position_size(buy, view, _portfolio(), PARAMETERS) == 0
    assert strategy.position_size(sell, view, _portfolio("7"), PARAMETERS) == 7
    assert strategy.position_size(sell, view, _portfolio("7.5"), PARAMETERS) == 0
    assert strategy.position_size(sell, view, _portfolio(), PARAMETERS) == 0


def test_multi_security_universe_holds_every_selected_security() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()

    signals = validate_entry_signals(
        strategy.entry_signals(
            view,
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl", "sec-msft"]},
        )
    )

    assert [signal.security_id for signal in signals] == ["sec-aapl", "sec-msft"]
    assert {signal.rule_id for signal in signals} == {"buy_and_hold_entry_v1"}


def test_empty_or_malformed_universe_emits_nothing() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()
    without_universe = {
        name: value
        for name, value in PARAMETERS.items()
        if name != "selected_securities"
    }

    assert strategy.entry_signals(view, {**PARAMETERS, "selected_securities": []}) == []
    assert (
        strategy.entry_signals(view, {**PARAMETERS, "selected_securities": "sec-aapl"})
        == []
    )
    assert strategy.entry_signals(view, without_universe) == []
