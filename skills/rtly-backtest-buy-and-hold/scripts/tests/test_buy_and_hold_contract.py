"""Contract tests for the buy-and-hold backtest Strategy."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Context, Decimal, localcontext
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pandas as pd

from app.services.backtest.strategy_evidence import (
    EVIDENCE_CONTRACT_VERSION,
    EvidenceKind,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_protocol import (
    InitialEntrySelectionProviderV1,
    PortfolioView,
    PositionSummaryV1,
    Signal,
    SignalSide,
    StrategyProtocolV1,
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
        history_sessions: int = 253,
    ) -> None:
        self.as_of_session = AS_OF
        index = (
            []
            if empty
            else [
                AS_OF - timedelta(days=history_sessions - offset)
                for offset in range(history_sessions)
            ]
        )
        values = [] if empty else [Decimal("100")] * history_sessions
        closes = (
            []
            if empty
            else [Decimal("100")] * (history_sessions - 1) + [Decimal(current_close)]
        )
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


def test_protocol_identity_and_one_shot_deterministic_selection() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()

    first = strategy.initial_entry_selection(view, PARAMETERS)
    second = strategy.initial_entry_selection(view, PARAMETERS)

    assert isinstance(strategy, StrategyProtocolV1)
    assert _MODULE.STRATEGY_ID == "rtly-backtest-buy-and-hold"
    assert _MODULE.STRATEGY_API_VERSION == 1
    assert first == second
    assert [(signal.side, signal.session) for signal in first.signals] == [
        (SignalSide.BUY, AS_OF)
    ]
    assert first.decisions[0].score == Decimal("0")


def test_strategy_provides_one_initial_selection_instead_of_ordinary_entries() -> None:
    strategy = BuyAndHoldStrategy()

    assert isinstance(strategy, InitialEntrySelectionProviderV1)
    assert strategy.entry_signals(_View(), PARAMETERS) == []


def test_cutoff_is_inclusive_and_invalid_or_future_cutoffs_fail_closed() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()

    assert (
        len(
            strategy.initial_entry_selection(
                view, {**PARAMETERS, "entry_on_or_after": AS_OF.isoformat()}
            ).signals
        )
        == 1
    )
    assert (
        strategy.initial_entry_selection(
            view,
            {
                **PARAMETERS,
                "entry_on_or_after": (AS_OF + timedelta(days=1)).isoformat(),
            },
        )
        .decisions[0]
        .reason_code
        == "entry_cutoff_not_reached"
    )
    assert (
        strategy.initial_entry_selection(
            view, {**PARAMETERS, "entry_on_or_after": "not-a-date"}
        )
        .decisions[0]
        .reason_code
        == "invalid_entry_cutoff"
    )


def test_missing_and_stale_target_history_fail_closed() -> None:
    strategy = BuyAndHoldStrategy()

    empty = strategy.initial_entry_selection(_View(empty=True), PARAMETERS)
    invalid = strategy.initial_entry_selection(_View(current_close="NaN"), PARAMETERS)

    assert empty.signals == ()
    assert empty.decisions[0].reason_code == "insufficient_history"
    assert invalid.signals == ()
    assert invalid.decisions[0].reason_code == "invalid_close_history"


def test_unavailable_history_has_a_distinct_stable_exclusion() -> None:
    strategy = BuyAndHoldStrategy()

    class _UnavailableView(_View):
        def price_history(self, security_id: str) -> None:
            del security_id
            return None

    selection = strategy.initial_entry_selection(_UnavailableView(), PARAMETERS)

    assert selection.signals == ()
    assert selection.decisions[0].reason_code == "history_unavailable"


def test_exactly_253_prior_closes_are_required() -> None:
    strategy = BuyAndHoldStrategy()

    eligible = strategy.initial_entry_selection(_View(history_sessions=253), PARAMETERS)
    short = strategy.initial_entry_selection(_View(history_sessions=252), PARAMETERS)

    assert len(eligible.signals) == 1
    assert short.signals == ()
    assert short.decisions[0].reason_code == "insufficient_history"


def test_zero_eligible_securities_returns_a_complete_excluded_batch() -> None:
    strategy = BuyAndHoldStrategy()
    selection = strategy.initial_entry_selection(
        _View(empty=True),
        {**PARAMETERS, "selected_securities": ["sec-b", "sec-a"]},
    )

    assert selection.signals == ()
    assert [decision.security_id for decision in selection.decisions] == [
        "sec-a",
        "sec-b",
    ]
    assert {decision.reason_code for decision in selection.decisions} == {
        "insufficient_history"
    }


def test_nullable_unused_volume_does_not_block_passive_entry() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()
    view._history.loc[AS_OF, "volume"] = None

    assert len(strategy.initial_entry_selection(view, PARAMETERS).signals) == 1


def test_strength_ignores_selection_day_and_future_rows() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View(current_close="200")
    baseline = strategy.initial_entry_selection(view, PARAMETERS).decisions[0].score
    view._history.loc[AS_OF] = [Decimal("999")] * 5
    view._history.loc[AS_OF + timedelta(days=1)] = [Decimal("1000")] * 5

    assert (
        strategy.initial_entry_selection(view, PARAMETERS).decisions[0].score
        == baseline
    )


def test_strength_is_independent_of_the_ambient_decimal_context() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View(current_close="101")
    view._history.iloc[0, view._history.columns.get_loc("close")] = Decimal("97")

    baseline = strategy.initial_entry_selection(view, PARAMETERS).decisions[0].score
    with localcontext(Context(prec=2)):
        constrained = (
            strategy.initial_entry_selection(view, PARAMETERS).decisions[0].score
        )

    assert constrained == baseline


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
    buy = strategy.initial_entry_selection(view, PARAMETERS).signals[0]
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


def test_multi_security_universe_ranks_top_x_by_score_then_id() -> None:
    strategy = BuyAndHoldStrategy()
    view = _View()

    selection = strategy.initial_entry_selection(
        view,
        {
            **PARAMETERS,
            "selected_securities": ["sec-msft", "sec-aapl", "sec-msft"],
            "top_x": 1,
        },
    )

    assert [signal.security_id for signal in selection.signals] == ["sec-aapl"]
    assert [decision.security_id for decision in selection.decisions] == [
        "sec-aapl",
        "sec-msft",
    ]


def test_top_x_selects_the_highest_return_not_input_order() -> None:
    strategy = BuyAndHoldStrategy()
    low, high = _View(current_close="110"), _View(current_close="200")

    class _PerSecurityView(_View):
        def price_history(self, security_id: str) -> pd.DataFrame:
            return (high if security_id == "sec-high" else low)._history.copy()

    selection = strategy.initial_entry_selection(
        _PerSecurityView(),
        {
            **PARAMETERS,
            "selected_securities": ["sec-low", "sec-high"],
            "top_x": 1,
        },
    )

    assert [signal.security_id for signal in selection.signals] == ["sec-high"]
    assert [decision.security_id for decision in selection.decisions] == [
        "sec-high",
        "sec-low",
    ]


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

    def scan_result(self, security_id: str) -> object:
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
    strategy = BuyAndHoldStrategy()

    absent = strategy.initial_entry_selection(_View(), PARAMETERS)
    disabled = strategy.initial_entry_selection(
        _View(), {**PARAMETERS, REGIME_FILTER_ENABLED_PARAM: False}
    )

    assert absent == disabled
    assert len(absent.signals) == 1


def test_regime_filter_suppresses_entries_but_not_exits_when_risk_off() -> None:
    strategy = BuyAndHoldStrategy()
    params = _risk_off_params()
    gated = _RegimeView(_View(), ["10", "10", "4"])

    selection = strategy.initial_entry_selection(gated, params)
    assert selection.signals == ()
    assert {decision.reason_code for decision in selection.decisions} == {
        "regime_filter_not_permitted"
    }
    assert validate_exit_signals(
        strategy.exit_signals(gated, _portfolio("10"), params)
    ) == validate_exit_signals(strategy.exit_signals(_View(), _portfolio("10"), params))


def test_regime_filter_fails_closed_when_benchmark_not_in_universe() -> None:
    strategy = BuyAndHoldStrategy()
    params = {
        **PARAMETERS,
        REGIME_FILTER_ENABLED_PARAM: True,
        REGIME_FILTER_BENCHMARK_PARAM: _BENCHMARK_ID,
        REGIME_FILTER_MA_LENGTH_PARAM: 3,
    }

    selection = strategy.initial_entry_selection(_View(), params)
    assert selection.signals == ()
    assert selection.decisions[0].reason_code == "regime_filter_not_permitted"


def test_regime_filter_enabled_risk_on_does_not_alter_entries() -> None:
    """Gate permits: enabled + risk-on entries match the disabled path."""
    strategy = BuyAndHoldStrategy()
    enabled = _risk_off_params()
    disabled = {**enabled, REGIME_FILTER_ENABLED_PARAM: False}

    assert strategy.initial_entry_selection(
        _RegimeView(_View(), ["1", "1", "100"]), enabled
    ) == strategy.initial_entry_selection(
        _RegimeView(_View(), ["1", "1", "100"]), disabled
    )


def test_evidence_requirements_declare_the_ranking_window() -> None:
    """Entry needs the 253 scored closes plus today; exit needs nothing."""
    requirements = BuyAndHoldStrategy().evidence_requirements(PARAMETERS)

    assert isinstance(requirements, StrategyEvidenceRequirementsV1)
    assert requirements.contract_version == EVIDENCE_CONTRACT_VERSION
    assert requirements.exit == ()
    assert len(requirements.entry) == 1
    entry = requirements.entry[0]
    assert entry.kind is EvidenceKind.PRICE_HISTORY
    assert entry.minimum_sessions == 254
    assert entry.columns == ("close",)
