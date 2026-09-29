"""Tests for the Strategy-derived stop suggestion and its tab wiring."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest

from app.schemas.record import StockRecord
from app.schemas.strategy_assignment import AssignmentView, StrategyAssignment
from app.schemas.trade import Position
from app.services.backtest.strategy_protocol import PortfolioView, PositionSummaryV1
from app.services.stop_suggestion import (
    BUY_AND_HOLD,
    BUY_AND_HOLD_NOTE,
    DARVAS_BOX,
    MAX_LOSS_NOTE,
    MINERVINI,
    MOVING_AVERAGE,
    NO_RECORD_NOTE,
    TURTLE_TREND,
    WEINSTEIN,
    suggest_stop,
)
from app.services.strategy_assignment_service import StrategyAssignmentService
from tests.test_portfolio_service import _make_service


def _position(**overrides: Any) -> Position:
    fields: dict[str, Any] = {
        "ticker": "AAA",
        "shares": 10,
        "avg_cost": 100.0,
        "total_cost": 1000.0,
        "current_price": 100.0,
    }
    return Position(**(fields | overrides))


def _record(
    sma50: float | None = 95.0,
    currency: str = "GBP",
    as_of: str = "2026-09-28",
    sma150: float | None = None,
    ohlcv: list[dict[str, float | int | str]] | None = None,
) -> StockRecord:
    return StockRecord(
        ticker="AAA",
        as_of=as_of,
        price=100.0,
        volume=1000,
        rel_volume=1.0,
        high_52w=120.0,
        low_52w=80.0,
        pct_from_52w_high=-10.0,
        pct_change_week=1.0,
        sma50=sma50,
        sma150=sma150,
        currency=currency,
        ohlcv_history=ohlcv or [],
    )


_MINERVINI_PARAMS = {"maximum_loss_pct": 8.0}


def test_minervini_sma50_binds() -> None:
    s = suggest_stop(_position(), _record(95.0), MINERVINI, _MINERVINI_PARAMS, None)
    assert s.level == 95.0
    assert s.rule == "50-day avg"
    assert s.distance_pct == pytest.approx(-5.0)
    assert not s.at_or_below


def test_minervini_max_loss_binds() -> None:
    s = suggest_stop(_position(), _record(80.0), MINERVINI, _MINERVINI_PARAMS, None)
    assert s.level == pytest.approx(92.0)
    assert s.rule == "max loss 8%"
    assert s.note == MAX_LOSS_NOTE


@pytest.mark.parametrize("record", [None, _record(None), _record(0.0)])
def test_minervini_without_sma50_uses_max_loss_with_note(
    record: StockRecord | None,
) -> None:
    s = suggest_stop(_position(), record, MINERVINI, _MINERVINI_PARAMS, None)
    assert s.level == pytest.approx(92.0)
    assert s.rule == "max loss 8%"
    assert s.note == f"50-day average unavailable · {MAX_LOSS_NOTE}"


def test_minervini_drops_sma50_in_another_currency() -> None:
    s = suggest_stop(
        _position(), _record(95.0, "USD"), MINERVINI, _MINERVINI_PARAMS, None
    )
    assert s.level == pytest.approx(92.0)
    assert s.note == f"50-day average is in another currency · {MAX_LOSS_NOTE}"


def test_missing_parameter_uses_descriptor_default() -> None:
    s = suggest_stop(_position(), None, WEINSTEIN, {}, {"maximum_loss_pct": 10.0})
    assert s.level == pytest.approx(90.0)
    assert s.rule == "max loss 10%"


def test_missing_parameter_without_defaults_gives_no_suggestion() -> None:
    s = suggest_stop(_position(), _record(), MINERVINI, {}, None)
    assert s.level is None
    assert s.note == "Maximum-loss setting unavailable"


def test_weinstein_uses_max_loss_only() -> None:
    s = suggest_stop(
        _position(), _record(99.0), WEINSTEIN, {"maximum_loss_pct": 10.0}, None
    )
    assert s.level == pytest.approx(90.0)
    assert s.rule == "max loss 10%"
    assert s.distance_pct == pytest.approx(-10.0)


def test_other_strategy_declares_no_rule() -> None:
    s = suggest_stop(_position(), _record(), "rtly-backtest-other", {}, None, "Other")
    assert s.level is None
    assert s.note == "No price stop rule for Other"


def test_no_assignment_declares_reason() -> None:
    s = suggest_stop(_position(), _record(), None, {}, None)
    assert s.level is None
    assert s.note == "No Strategy assigned"


@pytest.mark.parametrize("price_currency", ["USD", "HKD"])
def test_mixed_units_give_no_suggestion(price_currency: str) -> None:
    pos = _position(price_currency=price_currency, cost_currency="GBP")
    s = suggest_stop(pos, _record(), MINERVINI, _MINERVINI_PARAMS, None)
    assert s.level is None
    assert s.note == "Cost and price are in different units"


@pytest.mark.parametrize("avg_cost", [0.0, -5.0, float("nan"), float("inf")])
def test_unusable_avg_cost_gives_no_suggestion(avg_cost: float) -> None:
    pos = _position(avg_cost=avg_cost)
    s = suggest_stop(pos, _record(), MINERVINI, _MINERVINI_PARAMS, None)
    assert s.level is None


def test_price_at_or_below_suggestion_is_flagged() -> None:
    pos = _position(current_price=90.0)
    s = suggest_stop(pos, _record(95.0), MINERVINI, _MINERVINI_PARAMS, None)
    assert s.level == 95.0
    assert s.at_or_below
    assert s.distance_pct == pytest.approx(95 / 90 * 100 - 100)


def test_unpriced_holding_has_no_distance() -> None:
    pos = _position(current_price=None)
    s = suggest_stop(pos, _record(), MINERVINI, _MINERVINI_PARAMS, None)
    assert s.level == 95.0
    assert s.distance_pct is None
    assert not s.at_or_below


@pytest.mark.parametrize("record_currency", ["GBp", "GBX"])
def test_pence_record_is_not_treated_as_pounds(record_currency: str) -> None:
    """A pence 50-day average must never bind against a pounds position."""
    s = suggest_stop(
        _position(price_currency="GBP", cost_currency="GBP"),
        _record(95.0, record_currency),
        MINERVINI,
        _MINERVINI_PARAMS,
        None,
    )
    assert s.level == pytest.approx(92.0)
    assert s.note == f"50-day average in a different unit · {MAX_LOSS_NOTE}"


def test_stale_sma50_falls_back_to_max_loss() -> None:
    s = suggest_stop(
        _position(),
        _record(95.0, as_of="2026-09-17"),
        MINERVINI,
        _MINERVINI_PARAMS,
        None,
        as_of=date(2026, 9, 28),
    )
    assert s.level == pytest.approx(92.0)
    assert s.note == f"50-day average is stale · {MAX_LOSS_NOTE}"


def test_sma50_within_ten_days_still_binds() -> None:
    s = suggest_stop(
        _position(),
        _record(95.0, as_of="2026-09-18"),
        MINERVINI,
        _MINERVINI_PARAMS,
        None,
        as_of=date(2026, 9, 28),
    )
    assert s.level == 95.0
    assert s.note is None


def test_zero_max_loss_stops_at_average_cost() -> None:
    s = suggest_stop(_position(), None, WEINSTEIN, {"maximum_loss_pct": 0}, None)
    assert s.level == pytest.approx(100.0)
    assert s.rule == "max loss 0%"


@pytest.mark.parametrize("stored", [None, "8", -1.0, 100.0, float("nan"), True])
def test_unusable_stored_max_loss_uses_descriptor_default(stored: Any) -> None:
    s = suggest_stop(
        _position(),
        None,
        WEINSTEIN,
        {"maximum_loss_pct": stored},
        {"maximum_loss_pct": 10.0},
    )
    assert s.level == pytest.approx(90.0)
    assert s.rule == "max loss 10%"


def test_small_levels_keep_full_precision() -> None:
    """The level is never rounded: display rounds, "Use" posts it whole."""
    for avg_cost, level in ((0.004347826, 0.004), (0.0375, 0.0345)):
        s = suggest_stop(
            _position(avg_cost=avg_cost, current_price=None),
            None,
            WEINSTEIN,
            {"maximum_loss_pct": 8.0},
            None,
        )
        assert s.level == avg_cost * (1 - 8.0 / 100)
        assert s.level == pytest.approx(level, rel=1e-6)


# --- portfolio_partial_context wiring -----------------------------------------


class _FakeAssignments:
    """Just enough of ``StrategyAssignmentService`` for the tab context."""

    def __init__(
        self,
        parameters: dict[str, Any],
        choices: Any = (),
        available: bool = True,
        strategy_id: str = MINERVINI,
    ) -> None:
        self.view: AssignmentView | Exception = AssignmentView(
            assignment=StrategyAssignment(
                portfolio_id=1,
                strategy_id=strategy_id,
                parameters=parameters,
                assigned_at="now",
                updated_at="now",
            ),
            available=available,
            display_name="Minervini",
        )
        self.choices = choices
        self.list_calls = 0

    def assignment_view(self, portfolio_id: int) -> AssignmentView:
        if isinstance(self.view, Exception):
            raise self.view
        return self.view

    def freshness(self) -> str:
        return "fresh"

    def list_choices(self) -> Any:
        self.list_calls += 1
        if isinstance(self.choices, Exception):
            raise self.choices
        return self.choices


def _context(
    monkeypatch: pytest.MonkeyPatch,
    assignments: _FakeAssignments,
    positions: list[Position],
) -> dict[str, Any]:
    svc = _make_service(monkeypatch)
    svc._assignment_service = cast(StrategyAssignmentService, assignments)
    monkeypatch.setattr(svc, "load_analysis", lambda: [_record(95.0)])
    return svc.portfolio_partial_context(
        positions, gbpusd_rate=2.0, cash_balance=0.0, portfolio_id=1
    )


def test_context_suggests_for_unstopped_holdings_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = _FakeAssignments(_MINERVINI_PARAMS)
    stopped = _position(ticker="BBB", stop_loss=85.0)
    ctx = _context(monkeypatch, assignments, [_position(), stopped])

    assert set(ctx["suggested_stops"]) == {"AAA"}
    assert ctx["suggested_stops"]["AAA"].level == 95.0
    # Suggestions are not evidence: the Position keeps no stop.
    assert ctx["positions"][0].stop_loss is None
    # Stored parameters suffice, so discovery is never consulted.
    assert assignments.list_calls == 0


def test_context_falls_back_to_descriptor_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = SimpleNamespace(
        strategy_id=MINERVINI, default_parameters={"maximum_loss_pct": 8.0}
    )
    ctx = _context(monkeypatch, _FakeAssignments({}, (descriptor,)), [_position()])

    assert ctx["suggested_stops"]["AAA"].level == 95.0


def test_context_discovery_failure_declares_no_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = _FakeAssignments({}, RuntimeError("boom"))
    ctx = _context(monkeypatch, assignments, [_position()])

    suggestion = ctx["suggested_stops"]["AAA"]
    assert suggestion.level is None
    assert suggestion.note == "Maximum-loss setting unavailable"


def test_context_without_assignment_declares_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _make_service(monkeypatch)
    ctx = svc.portfolio_partial_context([_position()], cash_balance=0.0)

    assert ctx["suggested_stops"]["AAA"].note == "No Strategy assigned"


def test_context_unavailable_strategy_gives_no_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = _FakeAssignments(_MINERVINI_PARAMS, available=False)
    ctx = _context(monkeypatch, assignments, [_position()])

    suggestion = ctx["suggested_stops"]["AAA"]
    assert suggestion.level is None
    assert suggestion.note == "Strategy unavailable"


def test_context_assignment_lookup_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assignments = _FakeAssignments(_MINERVINI_PARAMS)
    assignments.view = RuntimeError("store unreadable")
    ctx = _context(monkeypatch, assignments, [_position()])

    assert ctx["strategy_assignment"] is None
    assert ctx["suggested_stops"]["AAA"].note == "Strategy unavailable"
    assert any(r.exc_info for r in caplog.records)


def test_context_unusable_stored_parameter_consults_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = SimpleNamespace(
        strategy_id=MINERVINI, default_parameters={"maximum_loss_pct": 8.0}
    )
    assignments = _FakeAssignments({"maximum_loss_pct": None}, (descriptor,))
    ctx = _context(monkeypatch, assignments, [_position()])

    assert assignments.list_calls == 1
    assert ctx["suggested_stops"]["AAA"].level == 95.0


def test_context_ages_sma50_against_the_newest_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _make_service(monkeypatch)
    svc._assignment_service = cast(
        StrategyAssignmentService, _FakeAssignments(_MINERVINI_PARAMS)
    )
    newer = _record(1.0, as_of="2026-09-28").model_copy(update={"ticker": "ZZZ"})
    stale = _record(95.0, as_of="2026-09-10")
    monkeypatch.setattr(svc, "load_analysis", lambda: [stale, newer])
    ctx = svc.portfolio_partial_context(
        [_position()], gbpusd_rate=2.0, cash_balance=0.0, portfolio_id=1
    )

    suggestion = ctx["suggested_stops"]["AAA"]
    assert suggestion.level == pytest.approx(92.0)
    assert suggestion.note is not None
    assert "50-day average is stale" in suggestion.note


def test_context_skips_oversold_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    assignments = _FakeAssignments(_MINERVINI_PARAMS)
    ctx = _context(monkeypatch, assignments, [_position(shares=-2)])

    assert ctx["suggested_stops"] == {}


# --- Weinstein: max-loss or 150-day average -----------------------------------

_WEINSTEIN_PARAMS = {"maximum_loss_pct": 10.0}


def test_weinstein_sma150_binds() -> None:
    record = _record(sma50=99.0, sma150=97.0)
    s = suggest_stop(_position(), record, WEINSTEIN, _WEINSTEIN_PARAMS, None)
    assert s.level == 97.0
    assert s.rule == "150-day avg"
    assert s.note is None


def test_weinstein_max_loss_binds_over_a_lower_sma150() -> None:
    record = _record(sma150=85.0)
    s = suggest_stop(_position(), record, WEINSTEIN, _WEINSTEIN_PARAMS, None)
    assert s.level == pytest.approx(90.0)
    assert s.rule == "max loss 10%"
    assert s.note == MAX_LOSS_NOTE


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (_record(sma150=None), "150-day average unavailable"),
        (_record(sma150=97.0, currency="GBp"), "150-day average in a different unit"),
        (
            _record(sma150=97.0, currency="USD"),
            "150-day average is in another currency",
        ),
        (_record(sma150=97.0, as_of="2026-09-17"), "150-day average is stale"),
    ],
)
def test_weinstein_unusable_sma150_falls_back_to_max_loss(
    record: StockRecord, reason: str
) -> None:
    s = suggest_stop(
        _position(), record, WEINSTEIN, _WEINSTEIN_PARAMS, None, as_of=date(2026, 9, 28)
    )
    assert s.level == pytest.approx(90.0)
    assert s.note == f"{reason} · {MAX_LOSS_NOTE}"


# --- price-history rules, checked against the Strategies' own exits -----------

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_AS_OF = date(2026, 9, 28)
_SECURITY = "AAA"


def _strategy(skill: str, class_name: str) -> Any:
    """Load a Strategy's runtime read-only, as its own contract tests do."""
    spec = spec_from_file_location(
        f"stop_{skill.replace('-', '_')}", _SKILLS / skill / "scripts/strategy.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)()


class _View:
    """A ``MarketViewV1`` over daily bars given oldest first."""

    def __init__(self, bars: list[dict[str, float]]) -> None:
        self.as_of_session = _AS_OF
        index = [_AS_OF - timedelta(days=len(bars) - 1 - i) for i in range(len(bars))]
        self._history = pd.DataFrame(bars, index=index)

    def price_history(
        self, security_id: str, *, limit: int | None = None, columns: Any = None
    ) -> pd.DataFrame:
        history = self._history if limit is None else self._history.iloc[-limit:]
        return history if columns is None else history.loc[:, list(columns)]

    def scan_result(self, security_id: str) -> None:
        return None


def _held() -> PortfolioView:
    return PortfolioView(
        as_of_session=_AS_OF,
        base_currency="GBP",
        cash=Decimal(1000),
        positions=(
            PositionSummaryV1(
                security_id=_SECURITY, quantity=Decimal(10), average_cost=Decimal(100)
            ),
        ),
        volatility_observations=(),
    )


def _bar(low: float, close: float | None = None) -> dict[str, float]:
    close = low + 2 if close is None else close
    return {"open": close, "high": close + 1, "low": low, "close": close}


def _newest_first(bars: list[dict[str, float]]) -> list[dict[str, float | int | str]]:
    """``ohlcv_history`` as the Scanner stores it: most recent first."""
    return [dict(bar, volume=1000) for bar in reversed(bars)]


def _exits(
    strategy: Any, bars: list[dict[str, float]], parameters: dict[str, Any]
) -> bool:
    """Whether ``strategy`` exits on the latest of ``bars``."""
    universe = {"selected_securities": [_SECURITY]}
    return bool(strategy.exit_signals(_View(bars), _held(), parameters | universe))


def _assert_exit_fires_below(
    strategy: Any,
    bars: list[dict[str, float]],
    parameters: dict[str, Any],
    level: float,
) -> None:
    """Tomorrow's bar at ``level`` - eps exits; at ``level`` + eps it holds."""
    eps = level * 1e-6
    assert _exits(strategy, [*bars, _bar(level - eps, level - eps)], parameters)
    assert not _exits(strategy, [*bars, _bar(level + eps, level + eps)], parameters)


# A deep low just outside each window: it must never set the level.
_OUTSIDE = _bar(10.0)
_WINDOW_LOWS = [97.0, 99.0, 96.5, 98.0]


@pytest.mark.parametrize(
    ("strategy_id", "skill", "class_name", "param", "rule"),
    [
        (
            DARVAS_BOX,
            "rtly-backtest-darvas-box",
            "DarvasBoxStrategy",
            "box_lookback_sessions",
            "box bottom (4-day low)",
        ),
        (
            TURTLE_TREND,
            "rtly-backtest-turtle-trend",
            "TurtleTrendStrategy",
            "exit_lookback_sessions",
            "4-day low",
        ),
    ],
)
def test_channel_low_is_the_level_the_strategy_exits_through(
    strategy_id: str, skill: str, class_name: str, param: str, rule: str
) -> None:
    bars = [_OUTSIDE, *(_bar(low) for low in _WINDOW_LOWS)]
    parameters = {param: 4}
    record = _record(ohlcv=_newest_first(bars))

    s = suggest_stop(_position(), record, strategy_id, parameters, None, as_of=_AS_OF)

    assert s.level == 96.5
    assert s.rule == rule
    assert s.note is None
    assert s.distance_pct == pytest.approx(-3.5)
    _assert_exit_fires_below(_strategy(skill, class_name), bars, parameters, 96.5)


def test_channel_window_uses_the_descriptor_default() -> None:
    bars = [_OUTSIDE, *(_bar(low) for low in _WINDOW_LOWS)]
    s = suggest_stop(
        _position(),
        _record(ohlcv=_newest_first(bars)),
        TURTLE_TREND,
        {"exit_lookback_sessions": "4"},
        {"exit_lookback_sessions": 3},
    )
    assert s.level == 96.5
    assert s.rule == "3-day low"


@pytest.mark.parametrize("strategy_id", [DARVAS_BOX, TURTLE_TREND])
def test_channel_without_a_usable_lookback_declares_it(strategy_id: str) -> None:
    s = suggest_stop(_position(), _record(), strategy_id, {}, None)
    assert s.level is None
    assert s.note == "Lookback setting unavailable"


_HISTORY_PARAMS = {
    DARVAS_BOX: {"box_lookback_sessions": 4},
    TURTLE_TREND: {"exit_lookback_sessions": 4},
    MOVING_AVERAGE: {"fast_window": 2, "slow_window": 4},
}
_FOUR_BARS = _newest_first([_bar(low) for low in _WINDOW_LOWS])


@pytest.mark.parametrize("strategy_id", list(_HISTORY_PARAMS))
@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (None, NO_RECORD_NOTE),
        (_record(ohlcv=_FOUR_BARS[:3]), "Needs 4 sessions of price history"),
        (_record(), "Needs 4 sessions of price history"),
        (
            _record(ohlcv=_FOUR_BARS, currency="GBp"),
            "Price history in a different unit",
        ),
        (
            _record(ohlcv=_FOUR_BARS, currency="USD"),
            "Price history is in another currency",
        ),
        (_record(ohlcv=_FOUR_BARS, as_of="2026-09-17"), "Price history is stale"),
        (
            _record(ohlcv=[{**_FOUR_BARS[0], "low": "x", "close": 0}, *_FOUR_BARS[1:]]),
            "Price history has unusable values",
        ),
    ],
)
def test_history_rules_without_usable_history_declare_why(
    strategy_id: str, record: StockRecord | None, reason: str
) -> None:
    s = suggest_stop(
        _position(),
        record,
        strategy_id,
        _HISTORY_PARAMS[strategy_id],
        None,
        as_of=_AS_OF,
    )
    assert s.level is None
    assert s.note == reason


# --- Moving Average: the crossover price --------------------------------------

_MA = ("rtly-backtest-moving-average", "MovingAverageStrategy")


def _closes(values: list[float]) -> list[dict[str, float]]:
    return [_bar(value - 1, value) for value in values]


@pytest.mark.parametrize(
    ("fast", "slow", "values"),
    [
        (2, 5, [100.0, 101.0, 103.0, 104.0, 106.0]),
        (3, 7, [90.0, 95.0, 100.0, 97.0, 99.0, 101.0, 98.5]),
        # Averages near each other, fast just above: a reachable crossover.
        (50, 200, [100.0] * 180 + [100.2] * 50),
    ],
)
def test_ma_crossover_price_is_where_the_strategy_exits(
    fast: int, slow: int, values: list[float]
) -> None:
    bars = _closes(values)
    parameters = {"fast_window": fast, "slow_window": slow}
    record = _record(ohlcv=_newest_first(bars))

    s = suggest_stop(_position(), record, MOVING_AVERAGE, parameters, None)

    assert s.level is not None
    assert s.rule == f"{fast}/{slow} crossover price"
    _assert_exit_fires_below(_strategy(*_MA), bars, parameters, s.level)


def test_ma_windows_default_from_the_descriptor() -> None:
    bars = _closes([100.0] * 150 + [100.2] * 50)
    s = suggest_stop(
        _position(),
        _record(ohlcv=_newest_first(bars)),
        MOVING_AVERAGE,
        {},
        {"fast_window": 50, "slow_window": 200},
    )
    assert s.rule == "50/200 crossover price"
    # (50 * Ss - 200 * Sf) / 150 with Ss = 149*100 + 50*100.2, Sf = 49*100.2.
    assert s.level == pytest.approx(13540 / 150)


def test_ma_needs_the_slow_window_of_history() -> None:
    bars = _closes([100.0 + i * 0.1 for i in range(199)])
    s = suggest_stop(
        _position(),
        _record(ohlcv=_newest_first(bars)),
        MOVING_AVERAGE,
        {"fast_window": 50, "slow_window": 200},
        None,
    )
    assert s.level is None
    assert s.note == "Needs 200 sessions of price history"


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        (
            [100.0, 100.0, 100.0, 10.0, 10.0],
            "Fast average already below slow — exit condition met",
        ),
        ([10.0, 10.0, 10.0, 10.0, 100.0], "No crossover price within reach"),
    ],
)
def test_ma_without_a_crossover_price_declares_why(
    values: list[float], reason: str
) -> None:
    s = suggest_stop(
        _position(),
        _record(ohlcv=_newest_first(_closes(values))),
        MOVING_AVERAGE,
        {"fast_window": 2, "slow_window": 5},
        None,
    )
    assert s.level is None
    assert s.note == reason


@pytest.mark.parametrize(
    "parameters",
    [{"fast_window": 5, "slow_window": 5}, {"fast_window": 5}, {"slow_window": 1}],
)
def test_ma_unusable_windows_give_no_suggestion(parameters: dict[str, Any]) -> None:
    s = suggest_stop(_position(), _record(), MOVING_AVERAGE, parameters, None)
    assert s.level is None
    assert s.note == "Moving-average windows unavailable"


# --- Buy and Hold: a default risk stop ----------------------------------------


def test_buy_and_hold_suggests_a_default_risk_stop() -> None:
    s = suggest_stop(_position(), None, BUY_AND_HOLD, {}, None)
    assert s.level == pytest.approx(90.0)
    assert s.rule == "default risk stop 10%"
    assert s.note == BUY_AND_HOLD_NOTE


def test_buy_and_hold_needs_a_same_unit_average_cost() -> None:
    pos = _position(price_currency="USD", cost_currency="GBP")
    s = suggest_stop(pos, None, BUY_AND_HOLD, {}, None)
    assert s.level is None
    assert s.note == "Cost and price are in different units"


def test_context_reads_defaults_for_a_missing_history_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = SimpleNamespace(
        strategy_id=TURTLE_TREND, default_parameters={"exit_lookback_sessions": 4}
    )
    assignments = _FakeAssignments({}, (descriptor,), strategy_id=TURTLE_TREND)
    svc = _make_service(monkeypatch)
    svc._assignment_service = cast(StrategyAssignmentService, assignments)
    record = _record(ohlcv=_newest_first([_bar(low) for low in _WINDOW_LOWS]))
    monkeypatch.setattr(svc, "load_analysis", lambda: [record])
    ctx = svc.portfolio_partial_context(
        [_position()], gbpusd_rate=2.0, cash_balance=0.0, portfolio_id=1
    )

    assert assignments.list_calls == 1
    assert ctx["suggested_stops"]["AAA"].level == 96.5
