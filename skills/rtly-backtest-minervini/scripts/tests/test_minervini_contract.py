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
    "selected_securities": ["sec-aapl"],
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
    score: int = 70,
    security_id: str = "sec-aapl",
) -> SimpleNamespace:
    return SimpleNamespace(
        security_id=security_id,
        as_of_session_date=as_of,
        stage=SimpleNamespace(value=stage),
        vcp=SimpleNamespace(
            valid_vcp=True,
            score=score,
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
    assert strategy.position_size(buy, view, portfolio, PARAMETERS) == 0


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


class _UniverseView(_View):
    """Serves the same evidence for every selected security."""

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        if self._scan is None:
            return None
        return SimpleNamespace(**{**vars(self._scan), "security_id": security_id})


def test_multi_security_universe_enters_each_qualifying_security() -> None:
    strategy = MinerviniStrategy()
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
    strategy = MinerviniStrategy()

    signals = validate_entry_signals(
        strategy.entry_signals(
            _View(_history(), _scan()),
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl"]},
        )
    )

    assert [signal.security_id for signal in signals] == ["sec-aapl"]


def test_multi_security_universe_exits_only_held_securities() -> None:
    strategy = MinerviniStrategy()
    view = _UniverseView(_history(), _scan(state="Damaged"))

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
        "upgrade_score_margin": 15,
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
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=95)},
    )

    exits = strategy.exit_signals(
        view,
        _held_portfolio(cash="1"),
        {**PARAMETERS, "selected_securities": ["sec-aapl", "sec-msft"]},
    )

    assert exits == []


def test_upgrade_exit_sells_weakest_position_when_margin_cleared() -> None:
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=95)},
    )

    exits = validate_exit_signals(
        strategy.exit_signals(view, _held_portfolio(cash="0"), _upgrade_parameters())
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("sec-aapl", "minervini_upgrade_exit_v1")
    ]


def test_upgrade_exit_keeps_a_cash_slot_for_engine_owned_buy_allocation() -> None:
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=95)},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="10000"), _upgrade_parameters()
    )

    assert exits == []


def test_upgrade_exit_does_not_fire_when_margin_not_cleared() -> None:
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=80)},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="1"), _upgrade_parameters()
    )

    assert exits == []


def test_upgrade_exit_never_duplicates_a_position_already_exiting_on_its_own_rule() -> (
    None
):
    """The held position is already exiting via its own risk rule
    (Damaged VCP) -- the upgrade check must not also emit a second SELL
    for the same security."""
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70, state="Damaged"), "sec-msft": _scan(score=95)},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="1"), _upgrade_parameters()
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("sec-aapl", "minervini_risk_exit_v1")
    ]


def test_empty_or_malformed_universe_emits_nothing() -> None:
    strategy = MinerviniStrategy()
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


# ---------------------------------------------------------------------------
# #388 -- opt-in market-regime entry filter
# ---------------------------------------------------------------------------

from app.services.backtest.regime_filter import (  # noqa: E402
    REGIME_FILTER_BENCHMARK_PARAM,
    REGIME_FILTER_ENABLED_PARAM,
    REGIME_FILTER_MA_LENGTH_PARAM,
)

_BENCHMARK_ID = "sec-spy"


class _RegimeView:
    """Wrap a contract ``_View`` and serve a crafted benchmark frame."""

    def __init__(self, inner: object, benchmark_closes: list[str]) -> None:
        self._inner = inner
        self.as_of_session = inner.as_of_session
        self._benchmark = pd.DataFrame(
            {"close": [Decimal(value) for value in benchmark_closes]}
        )

    def price_history(self, security_id: str) -> pd.DataFrame:
        if security_id == _BENCHMARK_ID:
            return self._benchmark.copy()
        return self._inner.price_history(security_id)

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        return self._inner.scan_result(security_id)


def _risk_off_params() -> dict[str, object]:
    return {
        **PARAMETERS,
        "selected_securities": ["sec-aapl", _BENCHMARK_ID],
        REGIME_FILTER_ENABLED_PARAM: True,
        REGIME_FILTER_BENCHMARK_PARAM: _BENCHMARK_ID,
        REGIME_FILTER_MA_LENGTH_PARAM: 3,
    }


def test_regime_filter_absent_matches_explicitly_disabled() -> None:
    strategy = MinerviniStrategy()

    absent = strategy.entry_signals(_View(_history(), _scan()), PARAMETERS)
    disabled = strategy.entry_signals(
        _View(_history(), _scan()), {**PARAMETERS, REGIME_FILTER_ENABLED_PARAM: False}
    )

    assert len(absent) == 1
    assert absent == disabled


def test_regime_filter_suppresses_entries_but_not_exits_when_risk_off() -> None:
    strategy = MinerviniStrategy()
    params = _risk_off_params()
    gated = _RegimeView(_View(_history(), _scan(state="Damaged")), ["10", "10", "4"])
    plain = _View(_history(), _scan(state="Damaged"))

    assert strategy.entry_signals(gated, params) == []
    assert validate_exit_signals(
        strategy.exit_signals(gated, _portfolio(), params)
    ) == validate_exit_signals(strategy.exit_signals(plain, _portfolio(), params))


def test_regime_filter_fails_closed_when_benchmark_not_in_universe() -> None:
    strategy = MinerviniStrategy()
    params = {
        **PARAMETERS,
        REGIME_FILTER_ENABLED_PARAM: True,
        REGIME_FILTER_BENCHMARK_PARAM: _BENCHMARK_ID,
        REGIME_FILTER_MA_LENGTH_PARAM: 3,
    }

    assert strategy.entry_signals(_View(_history(), _scan()), params) == []


def test_regime_filter_enabled_risk_on_does_not_alter_entries() -> None:
    """Gate permits: enabled + risk-on entries match the disabled path."""
    strategy = MinerviniStrategy()
    enabled = _risk_off_params()
    disabled = {**enabled, REGIME_FILTER_ENABLED_PARAM: False}

    assert strategy.entry_signals(
        _RegimeView(_View(_history(), _scan()), ["1", "1", "100"]), enabled
    ) == strategy.entry_signals(
        _RegimeView(_View(_history(), _scan()), ["1", "1", "100"]), disabled
    )
