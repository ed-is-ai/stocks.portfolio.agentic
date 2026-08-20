"""Contract tests for the deterministic Minervini backtest Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

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

RUNTIME = Path(__file__).resolve().parents[1] / "strategy.py"
SPEC = spec_from_file_location("minervini_backtest_strategy_test", RUNTIME)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MinerviniStrategy = MODULE.MinerviniStrategy

AS_OF = date(2026, 8, 20)
PARAMETERS = {
    "security_id": "sec-aapl",
    "fixed_shares": 10,
    "minimum_vcp_score": 70,
    "minimum_trend_score": 85.0,
    "minimum_relative_volume": 1.5,
    "maximum_pivot_extension_pct": 3.0,
    "maximum_loss_pct": 8.0,
}


def _history(
    *, current_close: str = "103", current_volume: str = "150"
) -> pd.DataFrame:
    sessions = pd.bdate_range(end=AS_OF, periods=51)
    closes = [Decimal("100")] * 50 + [Decimal(current_close)]
    volumes = [Decimal("100")] * 50 + [Decimal(current_volume)]
    return pd.DataFrame({"close": closes, "volume": volumes}, index=sessions)


def _scan(
    *,
    stage: str = "Stage 2",
    state: str = "Breakout",
    as_of: date = date(2026, 7, 31),
) -> SimpleNamespace:
    return SimpleNamespace(
        security_id="sec-aapl",
        as_of_session_date=as_of,
        stage=SimpleNamespace(value=stage),
        vcp=SimpleNamespace(
            valid_vcp=True,
            score=70,
            trend_template_score=Decimal("85"),
            trend_template_passed=True,
            breakout_volume_detected=True,
            pivot_price=Decimal("100"),
            execution_state=state,
        ),
    )


class _View:
    def __init__(self, history: pd.DataFrame, scan: SimpleNamespace | None) -> None:
        self.as_of_session = AS_OF
        self._history = history
        self._scan = scan

    def price_history(self, security_id: str) -> pd.DataFrame:
        return self._history

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        return self._scan


def _portfolio(quantity: str = "10", average_cost: str = "100") -> PortfolioView:
    return PortfolioView(
        as_of_session=AS_OF,
        base_currency="GBP",
        cash=Decimal("1000"),
        positions=(
            PositionSummaryV1(
                security_id="sec-aapl",
                quantity=Decimal(quantity),
                average_cost=Decimal(average_cost),
            ),
        ),
        volatility_observations=(),
    )


def test_entry_qualifies_at_inclusive_score_volume_and_extension_bounds() -> None:
    strategy = MinerviniStrategy()
    view = _View(_history(), _scan())

    first = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))
    second = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))

    assert isinstance(strategy, StrategyProtocolV1)
    assert first == second
    assert [(item.side, item.rule_id) for item in first] == [
        (SignalSide.BUY, "minervini_vcp_breakout_v1")
    ]


def test_entry_fails_closed_for_short_stale_or_overextended_evidence() -> None:
    strategy = MinerviniStrategy()
    short = _history().iloc[-50:]
    stale = _history().iloc[:-1]

    assert strategy.entry_signals(_View(short, _scan()), PARAMETERS) == []
    assert strategy.entry_signals(_View(stale, _scan()), PARAMETERS) == []
    assert (
        strategy.entry_signals(
            _View(_history(current_close="103.01"), _scan()), PARAMETERS
        )
        == []
    )


def test_entry_fails_closed_for_missing_future_or_invalid_volume_evidence() -> None:
    strategy = MinerviniStrategy()

    assert strategy.entry_signals(_View(_history(), None), PARAMETERS) == []
    assert (
        strategy.entry_signals(
            _View(_history(), _scan(as_of=date(2026, 8, 21))), PARAMETERS
        )
        == []
    )
    assert (
        strategy.entry_signals(
            _View(_history(current_volume="NaN"), _scan()), PARAMETERS
        )
        == []
    )
    assert (
        strategy.entry_signals(
            _View(_history(current_volume="149.99"), _scan()), PARAMETERS
        )
        == []
    )
    zero_volume = _history(current_volume="0")
    zero_volume.loc[:, "volume"] = Decimal("0")
    assert strategy.entry_signals(_View(zero_volume, _scan()), PARAMETERS) == []


def test_exit_and_position_sizing_use_full_integral_held_quantity() -> None:
    strategy = MinerviniStrategy()
    view = _View(_history(), _scan(state="Damaged"))
    portfolio = _portfolio()

    exits = validate_exit_signals(strategy.exit_signals(view, portfolio, PARAMETERS))

    assert len(exits) == 1
    assert (
        validate_position_size(
            strategy.position_size(exits[0], view, portfolio, PARAMETERS)
        )
        == 10
    )
    assert strategy.position_size(exits[0], view, _portfolio("10.5"), PARAMETERS) == 0
    buy = Signal(
        security_id="sec-aapl",
        side=SignalSide.BUY,
        session=AS_OF,
        rule_id="test_buy",
    )
    assert strategy.position_size(buy, view, portfolio, PARAMETERS) == 10


def test_price_risk_exit_does_not_require_scan_and_zero_quantity_does_not_sell() -> (
    None
):
    strategy = MinerviniStrategy()
    loss_view = _View(_history(current_close="91"), None)

    assert len(strategy.exit_signals(loss_view, _portfolio(), PARAMETERS)) == 1
    assert (
        strategy.exit_signals(
            _View(_history(), _scan(state="Damaged")), _portfolio("0"), PARAMETERS
        )
        == []
    )
