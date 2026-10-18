"""Contract tests for the 52-week-high proximity Strategy on synthetic bars."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd
import pytest

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

SEC = "sec-001"
FIRST_OF_MONTH = date(2026, 9, 1)
MID_MONTH = date(2026, 9, 15)
LOOKBACK = 20


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "strategy.py"
    spec = spec_from_file_location("fifty_two_week_high_strategy", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


strategy = _load().strategy


def _history(
    close: str = "96",
    *,
    as_of: date = FIRST_OF_MONTH,
    rows: int = LOOKBACK + 5,
    high: str = "100",
    older_highs: tuple[int, ...] = (),
) -> pd.DataFrame:
    """``rows`` sessions ending on ``as_of``; the window high is ``high``.

    ``older_highs`` rows (by position) get a high of 500.
    """
    index = [ts.date() for ts in pd.bdate_range(end=as_of, periods=rows)]
    highs = [Decimal("50")] * rows
    highs[-3] = Decimal(high)
    highs[-1] = Decimal(close)
    for position in older_highs:
        highs[position] = Decimal("500")
    closes = [Decimal("50")] * (rows - 1) + [Decimal(close)]
    return pd.DataFrame({"high": highs, "close": closes}, index=index)


class _View:
    """Serves one fixed history per security, ignoring ``limit``."""

    def __init__(
        self, histories: dict[str, pd.DataFrame], as_of: date = FIRST_OF_MONTH
    ) -> None:
        self.as_of_session = as_of
        self._histories = histories

    def price_history(self, security_id: str, **_: object) -> pd.DataFrame:
        return self._histories.get(security_id, pd.DataFrame()).copy()

    def scan_result(self, security_id: str) -> None:
        return None


def _one(history: pd.DataFrame, as_of: date = FIRST_OF_MONTH) -> _View:
    return _View({SEC: history}, as_of)


def _params(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "selected_securities": [SEC],
        "high_lookback_sessions": LOOKBACK,
        "entry_proximity": 0.95,
        "exit_proximity": 0.85,
        "rebalance_cadence": "daily",
        "maximum_loss_pct": 10.0,
    }
    return {**base, **over}


def _portfolio(held: bool, average_cost: str = "50") -> PortfolioView:
    positions = (
        (
            PositionSummaryV1(
                security_id=SEC, quantity=Decimal(7), average_cost=Decimal(average_cost)
            ),
        )
        if held
        else ()
    )
    return PortfolioView(
        as_of_session=FIRST_OF_MONTH,
        base_currency="GBP",
        cash=Decimal(1000),
        positions=positions,
        volatility_observations=(),
    )


def _codes(signals: Sequence[Signal]) -> tuple[str, ...]:
    assert len(signals) == 1 and signals[0].explanation is not None
    return signals[0].explanation.codes


def _with_close(history: pd.DataFrame, close: str) -> pd.DataFrame:
    """Return ``history`` with only the latest close replaced."""
    closes = [*history["close"].iloc[:-1], Decimal(close)]
    return pd.DataFrame({"high": history["high"], "close": closes}, history.index)


def test_close_above_every_window_high_caps_proximity_at_one() -> None:
    history = _with_close(_history("96"), "120")
    signals = strategy.entry_signals(_one(history), _params())
    assert [s.priority for s in signals] == [Decimal(1)]


@pytest.mark.parametrize("close", ["0", "-5"])
def test_non_positive_close_never_fires_the_stop(close: str) -> None:
    history = _with_close(_history("96", rows=3), close)
    exits = strategy.exit_signals(_one(history), _portfolio(True, "100"), _params())
    assert exits == []


def test_conforms_to_protocol() -> None:
    assert isinstance(strategy, StrategyProtocolV1)


def test_entry_near_high_sets_priority_to_proximity() -> None:
    signals = validate_entry_signals(
        strategy.entry_signals(_one(_history("96")), _params())
    )
    assert [(s.side, s.priority) for s in signals] == [
        (SignalSide.BUY, Decimal("0.96"))
    ]
    assert _codes(signals) == ("near_52_week_high",)


def test_entry_threshold_is_inclusive() -> None:
    assert strategy.entry_signals(_one(_history("95")), _params())
    assert strategy.entry_signals(_one(_history("94.99")), _params()) == []


def test_window_is_the_tail_ending_today() -> None:
    # The old high falls outside the latest LOOKBACK rows: today is the high.
    outside = _history("60", high="60", older_highs=(0, 1, 2, 3, 4))
    assert strategy.entry_signals(_one(outside), _params())
    inside = _history("60", high="60", older_highs=(5,))
    assert strategy.entry_signals(_one(inside), _params()) == []


def test_proximity_exit_when_held() -> None:
    exits = validate_exit_signals(
        strategy.exit_signals(_one(_history("84")), _portfolio(True), _params())
    )
    assert exits[0].side == SignalSide.SELL
    assert _codes(exits) == ("proximity_below_exit",)
    assert (
        strategy.exit_signals(_one(_history("85")), _portfolio(True), _params()) == []
    )
    assert (
        strategy.exit_signals(_one(_history("84")), _portfolio(False), _params()) == []
    )


def test_loss_stop_fires_with_short_history() -> None:
    held = _portfolio(True, average_cost="100")
    short = _history("90", rows=3)
    assert _codes(strategy.exit_signals(_one(short), held, _params())) == (
        "maximum_loss_stop",
    )
    assert strategy.exit_signals(_one(_history("90.01", rows=3)), held, _params()) == []


def test_stop_and_proximity_exit_share_one_sell() -> None:
    held = _portfolio(True, average_cost="100")
    exits = strategy.exit_signals(_one(_history("80")), held, _params())
    assert _codes(exits) == ("maximum_loss_stop", "proximity_below_exit")


@pytest.mark.parametrize("exit_proximity", [0.95, 0.99])
def test_inverted_thresholds_fail_closed(exit_proximity: float) -> None:
    params = _params(exit_proximity=exit_proximity)
    held = _portfolio(True, average_cost="100")
    assert strategy.entry_signals(_one(_history("99")), params) == []
    assert strategy.exit_signals(_one(_history("1")), held, params) == []
    level = strategy.stop_level(_one(_history()), held, params, SEC)
    assert level is not None and level.rule_code == "invalid_setting"


@pytest.mark.parametrize(
    "over",
    [
        {"high_lookback_sessions": True},
        {"high_lookback_sessions": 1},
        {"entry_proximity": None},
        {"entry_proximity": 1.5},
        {"entry_proximity": "nan"},
        {"exit_proximity": -0.1},
        {"maximum_loss_pct": 100},
        {"maximum_loss_pct": True},
        {"rebalance_cadence": "weekly"},
        {"selected_securities": SEC},
    ],
)
def test_bad_parameters_emit_nothing(over: dict[str, object]) -> None:
    params = _params(**over)
    held = _portfolio(True, average_cost="100")
    assert strategy.entry_signals(_one(_history("99")), params) == []
    assert strategy.exit_signals(_one(_history("1")), held, params) == []


@pytest.mark.parametrize(
    "shape",
    [{"rows": LOOKBACK - 1}, {"as_of": date(2026, 8, 31)}, {"high": "0"}],
    ids=["short", "no_current_bar", "non_positive_high"],
)
def test_unusable_history_gives_no_proximity_signal(shape: dict[str, Any]) -> None:
    assert strategy.entry_signals(_one(_history("99", **shape)), _params()) == []
    low = _one(_history("60", **shape))
    assert strategy.exit_signals(low, _portfolio(True), _params()) == []


def test_empty_history_gives_no_signal() -> None:
    held = _portfolio(True, average_cost="100")
    assert strategy.entry_signals(_one(pd.DataFrame()), _params()) == []
    assert strategy.exit_signals(_one(pd.DataFrame()), held, _params()) == []


def test_monthly_cadence_gates_entry_and_proximity_exit_but_not_stop() -> None:
    params = _params(rebalance_cadence="monthly")
    mid = _one(_history("99", as_of=MID_MONTH), MID_MONTH)
    assert strategy.entry_signals(mid, params) == []
    low = _one(_history("84", as_of=MID_MONTH), MID_MONTH)
    assert strategy.exit_signals(low, _portfolio(True), params) == []
    stopped = _one(_history("80", as_of=MID_MONTH), MID_MONTH)
    held = _portfolio(True, average_cost="100")
    assert _codes(strategy.exit_signals(stopped, held, params)) == (
        "maximum_loss_stop",
    )
    assert strategy.entry_signals(_one(_history("99")), params)
    assert strategy.exit_signals(_one(_history("84")), _portfolio(True), params)


def test_one_security_gap_cannot_fake_a_monthly_rebalance() -> None:
    other = "sec-000"
    second = date(2026, 9, 2)
    gapped = _history("99", as_of=second).drop(index=FIRST_OF_MONTH)
    view = _View({SEC: gapped, other: _history("50", as_of=second)}, second)
    alone = _View({SEC: gapped}, second)
    params_alone = _params(rebalance_cadence="monthly")
    assert strategy.entry_signals(alone, params_alone)
    params = _params(selected_securities=[SEC, other], rebalance_cadence="monthly")
    assert strategy.entry_signals(view, params) == []


def test_regime_filter_blocks_entries() -> None:
    params = _params(
        block_buy_on_downtrend_enabled=True,
        regime_filter_benchmark_security_id="",
        regime_filter_ma_length=200,
    )
    assert strategy.entry_signals(_one(_history("99")), params) == []


def test_position_size() -> None:
    view = _one(_history("84"))
    sell = strategy.exit_signals(view, _portfolio(True), _params())[0]
    sized = strategy.position_size(sell, view, _portfolio(True), _params())
    assert validate_position_size(sized) == 7
    buy = strategy.entry_signals(_one(_history("99")), _params())[0]
    assert strategy.position_size(buy, view, _portfolio(False), _params()) == 0


def test_stop_level_matches_the_risk_stop() -> None:
    held = _portfolio(True, average_cost="100")
    view = _one(_history())
    level = strategy.stop_level(view, held, _params(), SEC)
    assert level is not None
    assert (level.level, level.basis, level.trigger) == (
        Decimal("90"),
        "average_cost",
        "close_lte",
    )
    assert strategy.stop_level(view, _portfolio(False), _params(), SEC) is None


def test_evidence_requirements_follow_the_lookback() -> None:
    requirements = strategy.evidence_requirements(_params())
    for requirement in requirements.entry + requirements.exit:
        assert requirement.minimum_sessions == LOOKBACK
        assert tuple(requirement.columns) == ("high", "close")
    fallback = strategy.evidence_requirements(_params(high_lookback_sessions=None))
    assert fallback.entry[0].minimum_sessions == 252


def test_signals_are_deterministic_across_runs_and_universe_order() -> None:
    other = "sec-000"
    view = _View({SEC: _history("99"), other: _history("97")})

    def run(universe: list[str]) -> list[dict[str, object]]:
        signals = strategy.entry_signals(view, _params(selected_securities=universe))
        return [signal.model_dump(mode="json") for signal in signals]

    first = run([SEC, other])
    assert first == run([other, SEC, SEC]) == run([SEC, other])
    assert [item["security_id"] for item in first] == [other, SEC]
