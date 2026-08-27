"""Contract tests for the deterministic Weinstein backtest Strategy."""

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
SPEC = spec_from_file_location("weinstein_backtest_strategy_test", RUNTIME)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
WeinsteinStrategy = MODULE.WeinsteinStrategy

AS_OF = date(2026, 8, 20)
PARAMETERS = {
    "selected_securities": ["sec-aapl"],
    "fixed_shares": 10,
    "breakout_lookback_sessions": 50,
    "minimum_relative_volume": 1.5,
    "maximum_loss_pct": 10.0,
}


def _history(
    *,
    current_close: str | None = None,
    current_high: str | None = None,
    current_volume: str = "150",
) -> pd.DataFrame:
    sessions = pd.bdate_range(end=AS_OF, periods=225)
    closes = [Decimal(100 + index) for index in range(225)]
    if current_close is not None:
        closes[-1] = Decimal(current_close)
    highs = list(closes)
    if current_high is not None:
        highs[-1] = Decimal(current_high)
    volumes = [Decimal("100")] * 224 + [Decimal(current_volume)]
    return pd.DataFrame(
        {"high": highs, "close": closes, "volume": volumes}, index=sessions
    )


def _scan(
    stage: str = "Stage 2", *, as_of: date = date(2026, 7, 31)
) -> SimpleNamespace:
    return SimpleNamespace(
        security_id="sec-aapl",
        as_of_session_date=as_of,
        stage=SimpleNamespace(value=stage),
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


def test_stage2_entry_qualifies_at_inclusive_volume_boundary() -> None:
    strategy = WeinsteinStrategy()
    view = _View(_history(), _scan())

    first = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))
    second = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))

    assert isinstance(strategy, StrategyProtocolV1)
    assert first == second
    assert [(item.side, item.rule_id) for item in first] == [
        (SignalSide.BUY, "weinstein_stage2_breakout_v1")
    ]


def test_entry_requires_strict_breakout_and_complete_current_history() -> None:
    strategy = WeinsteinStrategy()
    history = _history()
    equal_breakout = history.copy()
    equal_breakout.loc[equal_breakout.index[-1], "close"] = history["high"].iloc[-2]

    assert strategy.entry_signals(_View(equal_breakout, _scan()), PARAMETERS) == []
    assert strategy.entry_signals(_View(history.iloc[:-1], _scan()), PARAMETERS) == []
    assert strategy.entry_signals(_View(history.iloc[-203:], _scan()), PARAMETERS) == []


def test_entry_fails_closed_for_missing_future_or_invalid_volume_evidence() -> None:
    strategy = WeinsteinStrategy()

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


def test_non_stage2_exit_and_position_sizing_are_fail_closed() -> None:
    strategy = WeinsteinStrategy()
    view = _View(_history(), _scan("Stage 3"))
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
    strategy = WeinsteinStrategy()
    loss_view = _View(_history(current_close="89"), None)

    assert len(strategy.exit_signals(loss_view, _portfolio(), PARAMETERS)) == 1
    assert (
        strategy.exit_signals(
            _View(_history(), _scan("Stage 3")), _portfolio("0"), PARAMETERS
        )
        == []
    )


class _UniverseView(_View):
    """Serves the same evidence for every selected security."""

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        if self._scan is None:
            return None
        return SimpleNamespace(**{**vars(self._scan), "security_id": security_id})


def test_multi_security_universe_enters_each_qualifying_security() -> None:
    strategy = WeinsteinStrategy()
    view = _UniverseView(_history(), _scan())

    signals = validate_entry_signals(
        strategy.entry_signals(
            view,
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl", "sec-msft"]},
        )
    )

    assert [signal.security_id for signal in signals] == ["sec-aapl", "sec-msft"]


def test_multi_security_universe_skips_securities_without_matching_scan() -> None:
    """``_View`` only ever serves ``sec-aapl``'s scan, so a second selected
    security has no visible evidence of its own and must not enter."""
    strategy = WeinsteinStrategy()

    signals = validate_entry_signals(
        strategy.entry_signals(
            _View(_history(), _scan()),
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl"]},
        )
    )

    assert [signal.security_id for signal in signals] == ["sec-aapl"]


def test_multi_security_universe_exits_only_held_securities() -> None:
    strategy = WeinsteinStrategy()
    view = _UniverseView(_history(), _scan("Stage 3"))

    exits = validate_exit_signals(
        strategy.exit_signals(
            view,
            _portfolio(),
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl"]},
        )
    )

    assert [signal.security_id for signal in exits] == ["sec-aapl"]


class _KeyedView:
    """Serves distinct history/scan evidence per security_id, for
    upgrade-exit tests that need a weak held position and a stronger
    unheld candidate to differ."""

    def __init__(
        self,
        histories: dict[str, pd.DataFrame],
        scans: dict[str, SimpleNamespace | None],
    ) -> None:
        self.as_of_session = AS_OF
        self._histories = histories
        self._scans = scans

    def price_history(self, security_id: str) -> pd.DataFrame:
        return self._histories.get(security_id, pd.DataFrame())

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        scan = self._scans.get(security_id)
        if scan is None:
            return None
        return SimpleNamespace(**{**vars(scan), "security_id": security_id})


def _upgrade_parameters(**overrides: object) -> dict[str, object]:
    return {
        **PARAMETERS,
        "selected_securities": ["sec-aapl", "sec-msft"],
        "enable_position_upgrade": True,
        "upgrade_score_margin_pct": 10.0,
        **overrides,
    }


def _held_portfolio(cash: str, security_id: str = "sec-aapl") -> PortfolioView:
    return PortfolioView(
        as_of_session=AS_OF,
        base_currency="GBP",
        cash=Decimal(cash),
        positions=(
            PositionSummaryV1(
                security_id=security_id,
                quantity=Decimal("10"),
                average_cost=Decimal("100"),
            ),
        ),
        volatility_observations=(),
    )


def test_upgrade_exit_disabled_by_default_even_with_a_stronger_starved_candidate() -> (
    None
):
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = strategy.exit_signals(
        view,
        _held_portfolio(cash="1"),
        {**PARAMETERS, "selected_securities": ["sec-aapl", "sec-msft"]},
    )

    assert exits == []


def test_upgrade_exit_sells_weakest_position_when_cash_insufficient_and_margin_cleared() -> (
    None
):
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = validate_exit_signals(
        strategy.exit_signals(view, _held_portfolio(cash="1"), _upgrade_parameters())
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("sec-aapl", "weinstein_upgrade_exit_v1")
    ]


def test_upgrade_exit_does_not_fire_when_cash_already_covers_the_candidate() -> None:
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="100000"), _upgrade_parameters()
    )

    assert exits == []


def test_upgrade_exit_does_not_fire_when_margin_not_cleared() -> None:
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="325")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="1"), _upgrade_parameters()
    )

    assert exits == []


def test_upgrade_exit_never_duplicates_a_position_already_exiting_on_its_own_rule() -> (
    None
):
    """The held position is already exiting via its own Stage 3 rule --
    the upgrade check must not also emit a second SELL for the same
    security."""
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan("Stage 3"), "sec-msft": _scan()},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="1"), _upgrade_parameters()
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("sec-aapl", "weinstein_stage_exit_v1")
    ]


def test_empty_or_malformed_universe_emits_nothing() -> None:
    strategy = WeinsteinStrategy()
    view = _UniverseView(_history(), _scan())
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
