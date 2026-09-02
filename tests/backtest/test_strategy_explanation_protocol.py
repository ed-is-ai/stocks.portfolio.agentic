"""Every first-party Strategy explains every signal it emits (#472).

Discovery-driven: every ``skills/rtly-backtest-*/scripts/strategy.py``
runtime is globbed, loaded, and driven with crafted synthetic evidence
through an entry scenario and (where it has one) an exit scenario. A new
Strategy that emits an unexplained signal -- or one this file has no
scenario for -- fails here rather than shipping an unreadable
recommendation. Scenario evidence reuses the per-Skill contract tests'
own synthetic view/portfolio/scan shapes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from app.core.config import SKILLS_DIR
from app.services.backtest.strategy_protocol import (
    PortfolioView,
    PositionSummaryV1,
    Signal,
    validate_signal_explanations,
)

RUNTIMES: tuple[Path, ...] = tuple(
    sorted(SKILLS_DIR.glob("rtly-backtest-*/scripts/strategy.py"))
)


def _load(runtime: Path) -> ModuleType:
    spec = spec_from_file_location(f"{runtime.parents[1].name}_explanation", runtime)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strategy(module: ModuleType) -> Any:
    """Instantiate the single Strategy class a runtime module exposes."""
    candidates = [
        value
        for name, value in vars(module).items()
        if isinstance(value, type)
        and name.endswith("Strategy")
        and value.__module__ == module.__name__
    ]
    assert len(candidates) == 1, f"expected one Strategy class, got {candidates!r}"
    return candidates[0]()


def _portfolio(as_of: date, security_id: str = "sec-aapl") -> PortfolioView:
    return PortfolioView(
        as_of_session=as_of,
        base_currency="GBP",
        cash=Decimal("1000"),
        positions=(
            PositionSummaryV1(
                security_id=security_id,
                quantity=Decimal("10"),
                average_cost=Decimal("100"),
            ),
        ),
        volatility_observations=(),
    )


class _View:
    """A bounded view serving one history (and optional scan) per session."""

    def __init__(
        self,
        as_of: date,
        history: pd.DataFrame,
        scan: SimpleNamespace | None = None,
    ) -> None:
        self.as_of_session = as_of
        self._history = history
        self._scan = scan

    def price_history(self, security_id: str) -> pd.DataFrame:
        return self._history.copy()

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        return self._scan


# ---------------------------------------------------------------------------
# Per-Strategy scenarios
# ---------------------------------------------------------------------------

_WEINSTEIN_AS_OF = date(2026, 8, 20)
_SHORT_AS_OF = date(2026, 1, 8)


def _weinstein_history(current_close: str | None = None) -> pd.DataFrame:
    sessions = pd.bdate_range(end=_WEINSTEIN_AS_OF, periods=225)
    closes = [Decimal(100 + index) for index in range(225)]
    if current_close is not None:
        closes[-1] = Decimal(current_close)
    volumes = [Decimal("100")] * 224 + [Decimal("150")]
    return pd.DataFrame(
        {"high": list(closes), "close": closes, "volume": volumes}, index=sessions
    )


def _stage_scan(stage: str = "Stage 2") -> SimpleNamespace:
    return SimpleNamespace(
        security_id="sec-aapl",
        as_of_session_date=date(2026, 7, 31),
        stage=SimpleNamespace(value=stage),
    )


def _weinstein(module: ModuleType) -> tuple[list[Signal], list[Signal]]:
    strategy = _strategy(module)
    parameters = {
        "selected_securities": ["sec-aapl"],
        "breakout_lookback_sessions": 50,
        "minimum_relative_volume": 1.5,
        "maximum_loss_pct": 10.0,
    }
    entry_view = _View(_WEINSTEIN_AS_OF, _weinstein_history(), _stage_scan())
    exit_view = _View(
        _WEINSTEIN_AS_OF, _weinstein_history("89"), _stage_scan("Stage 3")
    )
    return (
        strategy.entry_signals(entry_view, parameters),
        strategy.exit_signals(exit_view, _portfolio(_WEINSTEIN_AS_OF), parameters),
    )


def _minervini_history(current_close: str = "103") -> pd.DataFrame:
    sessions = pd.bdate_range(end=_WEINSTEIN_AS_OF, periods=51)
    closes = [Decimal("100")] * 50 + [Decimal(current_close)]
    volumes = [Decimal("100")] * 50 + [Decimal("150")]
    return pd.DataFrame({"close": closes, "volume": volumes}, index=sessions)


def _vcp_scan(state: str = "Breakout") -> SimpleNamespace:
    return SimpleNamespace(
        security_id="sec-aapl",
        as_of_session_date=date(2026, 7, 31),
        stage=SimpleNamespace(value="Stage 2"),
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


def _minervini(module: ModuleType) -> tuple[list[Signal], list[Signal]]:
    strategy = _strategy(module)
    parameters = {
        "selected_securities": ["sec-aapl"],
        "minimum_vcp_score": 70,
        "minimum_trend_score": 85.0,
        "minimum_relative_volume": 1.5,
        "maximum_pivot_extension_pct": 3.0,
        "maximum_loss_pct": 8.0,
    }
    entry_view = _View(_WEINSTEIN_AS_OF, _minervini_history(), _vcp_scan())
    exit_view = _View(_WEINSTEIN_AS_OF, _minervini_history(), _vcp_scan("Damaged"))
    return (
        strategy.entry_signals(entry_view, parameters),
        strategy.exit_signals(exit_view, _portfolio(_WEINSTEIN_AS_OF), parameters),
    )


def _short_sessions(count: int = 4) -> list[date]:
    return [
        _SHORT_AS_OF - timedelta(days=count - 1 - offset) for offset in range(count)
    ]


def _darvas_history(current_close: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [Decimal("95")] * 4,
            "high": [Decimal("100"), Decimal("99"), Decimal("98"), Decimal("102")],
            "low": [Decimal("90"), Decimal("91"), Decimal("92"), Decimal("89")],
            "close": [
                Decimal("95"),
                Decimal("96"),
                Decimal("97"),
                Decimal(current_close),
            ],
            "volume": [Decimal("100")] * 3 + [Decimal("150")],
        },
        index=_short_sessions(),
    )


def _darvas(module: ModuleType) -> tuple[list[Signal], list[Signal]]:
    strategy = _strategy(module)
    parameters = {
        "selected_securities": ["sec-aapl"],
        "box_lookback_sessions": 3,
        "maximum_box_depth_pct": 10.0,
        "volume_multiplier": 1.5,
    }
    return (
        strategy.entry_signals(_View(_SHORT_AS_OF, _darvas_history("101")), parameters),
        strategy.exit_signals(
            _View(_SHORT_AS_OF, _darvas_history("89.99")),
            _portfolio(_SHORT_AS_OF),
            parameters,
        ),
    )


def _turtle_history(*, current_high: str, current_low: str) -> pd.DataFrame:
    return pd.DataFrame(
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
        index=_short_sessions(),
    )


def _turtle(module: ModuleType) -> tuple[list[Signal], list[Signal]]:
    strategy = _strategy(module)
    parameters = {
        "selected_securities": ["sec-aapl"],
        "entry_lookback_sessions": 3,
        "exit_lookback_sessions": 2,
    }
    entry_view = _View(
        _SHORT_AS_OF, _turtle_history(current_high="13", current_low="10")
    )
    exit_view = _View(_SHORT_AS_OF, _turtle_history(current_high="10", current_low="7"))
    return (
        strategy.entry_signals(entry_view, parameters),
        strategy.exit_signals(exit_view, _portfolio(_SHORT_AS_OF), parameters),
    )


def _moving_average_history(closes: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": [Decimal(value) for value in closes]},
        index=_short_sessions(len(closes)),
    )


def _moving_average(module: ModuleType) -> tuple[list[Signal], list[Signal]]:
    strategy = _strategy(module)
    parameters = {
        "selected_securities": ["sec-aapl"],
        "fast_window": 2,
        "slow_window": 3,
    }
    entry_view = _View(_SHORT_AS_OF, _moving_average_history(["3", "2", "1", "4"]))
    exit_view = _View(_SHORT_AS_OF, _moving_average_history(["1", "2", "3", "0"]))
    return (
        strategy.entry_signals(entry_view, parameters),
        strategy.exit_signals(exit_view, _portfolio(_SHORT_AS_OF), parameters),
    )


def _buy_and_hold(module: ModuleType) -> tuple[list[Signal], list[Signal]]:
    strategy = _strategy(module)
    parameters = {
        "selected_securities": ["sec-aapl"],
        "entry_on_or_after": "2000-01-01",
        "top_x": 1,
    }
    sessions = 253
    index = [
        _SHORT_AS_OF - timedelta(days=sessions - offset) for offset in range(sessions)
    ]
    values = [Decimal("100")] * sessions
    history = pd.DataFrame(
        {
            "open": values,
            "high": values,
            "low": values,
            "close": [Decimal("100")] * (sessions - 1) + [Decimal("150")],
            "volume": values,
        },
        index=index,
    )
    view = _View(_SHORT_AS_OF, history)
    selection = strategy.initial_entry_selection(view, parameters)
    return list(selection.signals), []


#: One scenario driver per first-party Strategy Skill folder. A newly
#: discovered Strategy without an entry here fails the coverage test
#: below rather than silently escaping the explanation contract.
SCENARIOS: dict[str, Callable[[ModuleType], tuple[list[Signal], list[Signal]]]] = {
    "rtly-backtest-buy-and-hold": _buy_and_hold,
    "rtly-backtest-darvas-box": _darvas,
    "rtly-backtest-minervini": _minervini,
    "rtly-backtest-moving-average": _moving_average,
    "rtly-backtest-turtle-trend": _turtle,
    "rtly-backtest-weinstein": _weinstein,
}


def test_every_first_party_strategy_runtime_is_discovered_and_covered() -> None:
    assert RUNTIMES, "no first-party Strategy runtimes were discovered"
    assert {runtime.parents[1].name for runtime in RUNTIMES} == set(SCENARIOS)


@pytest.mark.parametrize("runtime", RUNTIMES, ids=lambda path: path.parents[1].name)
def test_every_emitted_signal_carries_a_deterministic_explanation(
    runtime: Path,
) -> None:
    module = _load(runtime)
    scenario = SCENARIOS[runtime.parents[1].name]

    entries, exits = scenario(module)
    repeat_entries, repeat_exits = scenario(module)

    assert entries, f"{runtime.parents[1].name} emitted no entry signal"
    validate_signal_explanations(entries, method_name="entry_signals")
    validate_signal_explanations(exits, method_name="exit_signals")
    for signal in (*entries, *exits):
        explanation = signal.explanation
        assert explanation is not None
        assert explanation.codes == tuple(sorted(explanation.codes))
        assert all(reason.summary for reason in explanation.reasons)
    assert [signal.explanation for signal in entries] == [
        signal.explanation for signal in repeat_entries
    ]
    assert [signal.explanation for signal in exits] == [
        signal.explanation for signal in repeat_exits
    ]


@pytest.mark.parametrize("runtime", RUNTIMES, ids=lambda path: path.parents[1].name)
def test_strategies_with_an_exit_policy_explain_their_exits(runtime: Path) -> None:
    """Only buy-and-hold has no exit rule; every other Strategy must sell."""
    module = _load(runtime)
    name = runtime.parents[1].name
    _, exits = SCENARIOS[name](module)

    if name == "rtly-backtest-buy-and-hold":
        assert exits == []
        return
    assert exits, f"{name} emitted no exit signal"
