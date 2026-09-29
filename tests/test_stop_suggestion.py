"""Tests for the Strategy-derived stop suggestion and its tab wiring."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.schemas.record import StockRecord
from app.schemas.strategy_assignment import AssignmentView, StrategyAssignment
from app.schemas.trade import Position
from app.services.stop_suggestion import (
    MAX_LOSS_NOTE,
    MINERVINI,
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
    sma50: float | None = 95.0, currency: str = "GBP", as_of: str = "2026-09-28"
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
        currency=currency,
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
    s = suggest_stop(
        _position(), _record(), "rtly-backtest-turtle-trend", {}, None, "Turtle"
    )
    assert s.level is None
    assert s.note == "No price stop rule for Turtle"


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
        self, parameters: dict[str, Any], choices: Any = (), available: bool = True
    ) -> None:
        self.view: AssignmentView | Exception = AssignmentView(
            assignment=StrategyAssignment(
                portfolio_id=1,
                strategy_id=MINERVINI,
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
