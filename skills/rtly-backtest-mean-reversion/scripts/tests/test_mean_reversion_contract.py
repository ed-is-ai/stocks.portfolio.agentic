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
#: 39 closes rising by 2 (100..176): a long-term uptrend.
RISING = [100 + 2 * i for i in range(39)]
#: 39 closes falling by 2 (200..124): a long-term downtrend.
FALLING = [200 - 2 * i for i in range(39)]


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
        "trend_lookback_sessions": 30,
        "maximum_loss_pct": 8.0,
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
    view = _View({"a": RISING + [152], "b": RISING + [149]})
    signals = MeanReversionStrategy().entry_signals(view, _params())
    validate_entry_signals(signals)
    assert [s.security_id for s in signals] == ["b", "a"]
    assert [s.priority for s in signals] == [Decimal(2), Decimal(1)]
    assert all(s.side == SignalSide.BUY for s in signals)


def test_uptrend_and_oversold_emits_exactly_one_buy() -> None:
    view = _View({"a": RISING + [152], "b": RISING + [178]})
    signals = MeanReversionStrategy().entry_signals(view, _params())
    assert [(s.security_id, s.rule_id) for s in signals] == [
        ("a", "mean_reversion_oversold_v1")
    ]


def test_no_entry_when_oversold_in_downtrend() -> None:
    view = _View({"a": FALLING + [100], "b": RISING[::-1] + [70]})
    assert MeanReversionStrategy().entry_signals(view, _params()) == []


def test_no_entry_when_not_oversold_flat_stale_or_short() -> None:
    strategy = MeanReversionStrategy()
    stale = _View({"a": RISING + [152]}, latest=AS_OF - timedelta(days=1))
    single = _params(selected_securities=["a"])
    assert strategy.entry_signals(_View({"a": RISING}), single) == []
    assert strategy.entry_signals(_View({"a": [100] * 40}), single) == []
    assert strategy.entry_signals(_View({"a": (RISING + [152])[-30:]}), single) == []
    assert strategy.entry_signals(stale, single) == []


def test_invalid_settings_fail_closed() -> None:
    view = _View({"a": RISING + [152], "b": RISING + [149]})
    strategy = MeanReversionStrategy()
    bad = [
        {"entry_zscore": 0},
        {"lookback_sessions": True},
        {"entry_zscore": 3.01},  # above sqrt(lookback - 1) = 3: unreachable
        {"exit_zscore": -2.0},  # <= -entry_zscore: sells on the entry bar
        {"maximum_loss_pct": None},
        {"maximum_loss_pct": 100},
    ]
    for extra in bad:
        assert strategy.entry_signals(view, _params(**extra)) == [], extra
    no_loss = {k: v for k, v in _params().items() if k != "maximum_loss_pct"}
    assert strategy.entry_signals(view, no_loss) == []
    assert (
        strategy.exit_signals(view, _portfolio(AS_OF), _params(max_holding_sessions=0))
        == []
    )
    stop = strategy.stop_level(view, _portfolio(AS_OF), no_loss, "a")
    assert stop is not None and stop.rule_code == "invalid_setting"


def test_runs_are_deterministic() -> None:
    view = _View({"a": RISING + [152], "b": RISING + [149]})
    strategy = MeanReversionStrategy()
    first = strategy.entry_signals(view, _params())
    assert first and first == MeanReversionStrategy().entry_signals(view, _params())
    exit_view = _View({"a": FLAT, "b": FLAT})
    portfolio = _portfolio(AS_OF)
    assert strategy.exit_signals(exit_view, portfolio, _params()) == (
        strategy.exit_signals(exit_view, portfolio, _params())
    )


def test_exit_on_reversion() -> None:
    view = _View({"a": FLAT, "b": FLAT})
    signals = MeanReversionStrategy().exit_signals(view, _portfolio(AS_OF), _params())
    validate_exit_signals(signals)
    assert [(s.security_id, s.rule_id) for s in signals] == [
        ("a", "mean_reversion_reverted_v1")
    ]


def test_time_stop_only_after_max_holding_and_with_entry_session() -> None:
    view = _View({"a": FLAT[:-1] + [95], "b": FLAT})
    strategy = MeanReversionStrategy()
    held_3 = strategy.exit_signals(
        view, _portfolio(AS_OF - timedelta(days=2)), _params()
    )
    assert [s.rule_id for s in held_3] == ["mean_reversion_time_stop_v1"]
    explanation = held_3[0].explanation
    assert explanation is not None
    assert explanation.codes == ("time_stop",)
    fact = explanation.reasons[0].facts[0]
    assert fact.label == "Sessions held"
    assert (fact.observed, fact.threshold) == (Decimal(3), Decimal(3))
    assert (
        strategy.exit_signals(view, _portfolio(AS_OF - timedelta(days=1)), _params())
        == []
    )
    assert strategy.exit_signals(view, _portfolio(None), _params()) == []


def test_time_stop_fires_on_flat_prices_without_a_zscore() -> None:
    view = _View({"a": [100] * 10})
    signals = MeanReversionStrategy().exit_signals(
        view, _portfolio(AS_OF - timedelta(days=5)), _params()
    )
    assert [s.rule_id for s in signals] == ["mean_reversion_time_stop_v1"]


def test_risk_stop_exits_at_or_below_maximum_loss() -> None:
    strategy = MeanReversionStrategy()
    # average_cost 90, 8% -> stop 82.8; still deeply oversold, held 1 session.
    hit = strategy.exit_signals(
        _View({"a": FLAT[:-1] + [82]}), _portfolio(AS_OF), _params()
    )
    validate_exit_signals(hit)
    assert [s.rule_id for s in hit] == ["mean_reversion_risk_stop_v1"]
    explanation = hit[0].explanation
    assert explanation is not None and explanation.codes == ("maximum_loss_stop",)
    assert (
        strategy.exit_signals(
            _View({"a": FLAT[:-1] + [83]}), _portfolio(AS_OF), _params()
        )
        == []
    )


def test_stop_level_is_the_maximum_loss_stop() -> None:
    strategy = MeanReversionStrategy()
    view = _View({"a": FLAT})
    stop = strategy.stop_level(view, _portfolio(AS_OF), _params(), "a")
    assert stop is not None
    assert stop.level == Decimal("82.8")
    assert (stop.rule_code, stop.basis, stop.trigger) == (
        "maximum_loss_stop",
        "average_cost",
        "close_lte",
    )
    assert strategy.stop_level(view, _portfolio(AS_OF), _params(), "b") is None


def test_sell_sizes_exact_quantity_and_buy_defers_to_engine() -> None:
    view = _View({"a": RISING + [152]})
    strategy = MeanReversionStrategy()
    params = _params(selected_securities=["a"])
    buy = strategy.entry_signals(view, params)[0]
    portfolio = _portfolio(AS_OF)
    assert strategy.position_size(buy, view, portfolio, params) == 0
    sell = buy.model_copy(update={"side": SignalSide.SELL})
    assert strategy.position_size(sell, view, portfolio, params) == Decimal(5)


def test_evidence_requirements_cover_every_lookback() -> None:
    strategy = MeanReversionStrategy()
    assert strategy.evidence_requirements(_params()).entry[0].minimum_sessions == 31
    long_hold = strategy.evidence_requirements(_params(max_holding_sessions=40))
    assert long_hold.exit[0].minimum_sessions == 41
    long_z = strategy.evidence_requirements(
        _params(lookback_sessions=50, trend_lookback_sessions=10)
    )
    assert long_z.entry[0].minimum_sessions == 50
