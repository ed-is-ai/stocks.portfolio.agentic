"""Contract tests for the mean-reversion backtest Strategy."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pandas as pd

from app.services.backtest.strategy_protocol import (
    PortfolioView,
    PositionSummaryV1,
    SignalSide,
    StrategyProtocolV1,
    validate_entry_signals,
    validate_exit_signals,
)

_RUNTIME = Path(__file__).resolve().parents[1] / "strategy.py"
_SPEC = spec_from_file_location("mean_reversion_backtest_strategy", _RUNTIME)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
MeanReversionStrategy = _MODULE.MeanReversionStrategy

AS_OF = date(2026, 1, 30)
FLAT = [100, 101, 99, 100, 101, 99, 100, 101, 99, 100]


class _View:
    def __init__(self, series: dict[str, list[int]], *, latest: date = AS_OF) -> None:
        self.as_of_session = AS_OF
        self._frames = {}
        for security_id, closes in series.items():
            start = latest - timedelta(days=len(closes) - 1)
            index = [start + timedelta(days=i) for i in range(len(closes))]
            self._frames[security_id] = pd.DataFrame({"close": closes}, index=index)

    def price_history(self, security_id, *, limit=None, columns=None):
        return self._frames[security_id].iloc[-limit:]

    def scan_result(self, security_id: str) -> None:
        return None


def _params(**extra: object) -> dict[str, object]:
    return {
        "selected_securities": ["a", "b"],
        "lookback_sessions": 10,
        "entry_zscore": 2.0,
        "exit_zscore": 0.0,
        "max_holding_sessions": 3,
        **extra,
    }


def _portfolio(entry: date | None, security_id: str = "a") -> PortfolioView:
    return PortfolioView(
        as_of_session=AS_OF,
        base_currency="GBP",
        cash=Decimal(0),
        positions=[
            PositionSummaryV1(
                security_id=security_id,
                quantity=Decimal(5),
                average_cost=Decimal(90),
                entry_session=entry,
            )
        ],
        volatility_observations=(),
    )


def test_satisfies_protocol() -> None:
    assert isinstance(MeanReversionStrategy(), StrategyProtocolV1)


def test_buys_oversold_ranked_by_depth() -> None:
    view = _View({"a": FLAT[:-1] + [90], "b": FLAT[:-1] + [80]})
    signals = MeanReversionStrategy().entry_signals(view, _params())
    validate_entry_signals(signals)
    assert [s.security_id for s in signals] == ["b", "a"]
    assert [s.priority for s in signals] == [Decimal(2), Decimal(1)]
    assert all(s.side == SignalSide.BUY for s in signals)


def test_no_entry_when_not_oversold_flat_stale_or_short() -> None:
    stale = _View({"a": FLAT[:-1] + [90]}, latest=AS_OF - timedelta(days=1))
    cases = [
        _View({"a": FLAT, "b": FLAT}),
        _View({"a": [100] * 10, "b": [100] * 9 + [50]}),
        _View({"a": FLAT[:-1] + [90], "b": [90] * 3}),
    ]
    assert MeanReversionStrategy().entry_signals(cases[0], _params()) == []
    assert [
        s.security_id
        for s in MeanReversionStrategy().entry_signals(cases[1], _params())
    ] == ["b"]
    assert [
        s.security_id
        for s in MeanReversionStrategy().entry_signals(cases[2], _params())
    ] == ["a"]
    assert (
        MeanReversionStrategy().entry_signals(stale, _params(selected_securities=["a"]))
        == []
    )


def test_invalid_settings_fail_closed() -> None:
    view = _View({"a": FLAT[:-1] + [90], "b": FLAT})
    strategy = MeanReversionStrategy()
    assert strategy.entry_signals(view, _params(entry_zscore=0)) == []
    assert strategy.entry_signals(view, _params(lookback_sessions=True)) == []
    assert (
        strategy.exit_signals(view, _portfolio(AS_OF), _params(max_holding_sessions=0))
        == []
    )


def test_exit_on_reversion() -> None:
    view = _View({"a": FLAT, "b": FLAT})
    signals = MeanReversionStrategy().exit_signals(view, _portfolio(AS_OF), _params())
    validate_exit_signals(signals)
    assert [(s.security_id, s.rule_id) for s in signals] == [
        ("a", "mean_reversion_reverted_v1")
    ]


def test_time_stop_only_after_max_holding_and_with_entry_session() -> None:
    view = _View({"a": FLAT[:-1] + [90], "b": FLAT})
    strategy = MeanReversionStrategy()
    held_3 = strategy.exit_signals(
        view, _portfolio(AS_OF - timedelta(days=2)), _params()
    )
    assert [s.rule_id for s in held_3] == ["mean_reversion_time_stop_v1"]
    assert (
        strategy.exit_signals(view, _portfolio(AS_OF - timedelta(days=1)), _params())
        == []
    )
    assert strategy.exit_signals(view, _portfolio(None), _params()) == []


def test_sell_sizes_exact_quantity_and_buy_defers_to_engine() -> None:
    view = _View({"a": FLAT[:-1] + [90]})
    strategy = MeanReversionStrategy()
    params = _params(selected_securities=["a"])
    buy = strategy.entry_signals(view, params)[0]
    portfolio = _portfolio(AS_OF)
    assert strategy.position_size(buy, view, portfolio, params) == 0
    sell = buy.model_copy(update={"side": SignalSide.SELL})
    assert strategy.position_size(sell, view, portfolio, params) == Decimal(5)


def test_evidence_requirements_follow_lookback() -> None:
    req = MeanReversionStrategy().evidence_requirements(_params())
    assert req.entry[0].minimum_sessions == 10
    assert req.exit[0].minimum_sessions == 10
