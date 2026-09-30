"""Tests for the Stop column's suggestion from the Strategy's own stop level.

The level comes from the assigned Strategy's ``stop_level`` via the
recommendation result (the Strategy skills' own tests prove the rules); the
host only guards units, declares reasons and renders the agents partial cell.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest

from app.api.templating import templates
from app.schemas.portfolio_recommendation import NO_ASSIGNMENT, EvaluationUnavailable
from app.schemas.strategy_assignment import AssignmentView
from app.schemas.trade import Position
from app.services.portfolio_agent_view import (
    RecommendationOutcome,
    build_agent_view,
    suggest_stop,
)
from app.services.strategy_assignment_service import StrategyAssignmentService
from tests._stop_helpers import result, stop_level
from tests.test_portfolio_service import _make_service


def _position(**overrides: Any) -> Position:
    fields: dict[str, Any] = {
        "ticker": "AAA",
        "shares": 10.0,
        "avg_cost": 100.0,
        "total_cost": 1000.0,
        "current_price": 100.0,
    }
    return Position(**(fields | overrides))


def test_level_is_suggested_with_rule_and_distance() -> None:
    s = suggest_stop(_position(), result(AAA=stop_level()))

    assert s.level == Decimal("95")
    assert s.rule == "Close below the 50-day average"
    assert s.facts == ("Close that breaks the 50-session SMA: 95",)
    assert s.distance_pct == pytest.approx(-5.0)
    assert s.breach == "" and s.stale == ""


def test_breach_mirrors_the_rules_own_comparison() -> None:
    def breach(price: float, trigger: str) -> str:
        level = stop_level(trigger=trigger)
        return suggest_stop(_position(current_price=price), result(AAA=level)).breach

    # A close at the level fires only an "at or below" rule.
    assert breach(95.0, "close_lte") == "Price at or below the suggested stop"
    assert breach(95.0, "close_lt") == ""
    assert breach(95.0, "low_lt") == ""
    assert breach(94.0, "close_lt") == "Price below the suggested stop"
    assert breach(94.0, "low_lt") == "Price below the suggested stop"
    assert breach(96.0, "close_lte") == ""


def test_unpriced_has_no_distance_or_breach() -> None:
    unpriced = suggest_stop(_position(current_price=None), result(AAA=stop_level()))

    assert unpriced.distance_pct is None and unpriced.breach == ""


def test_stale_scan_keeps_the_level_with_a_note() -> None:
    for freshness in ("stale", "unknown"):
        s = suggest_stop(_position(), result(freshness, AAA=stop_level()))

        assert s.level == Decimal("95")
        assert s.stale == "Based on a stale scan (as of 25 Sep 2026)"


@pytest.mark.parametrize(
    ("outcome", "note"),
    [
        (NO_ASSIGNMENT, "No Strategy assigned"),
        (EvaluationUnavailable(reason="boom"), "Strategy unavailable"),
        (result(), "No stop level from the Strategy"),
        (
            result(
                AAA=stop_level(None, summary="Needs 49 sessions of current closes.")
            ),
            "Needs 49 sessions of current closes.",
        ),
    ],
)
def test_declared_reasons(outcome: RecommendationOutcome, note: str) -> None:
    s = suggest_stop(_position(), outcome)

    assert s.level is None
    assert s.note == note


@pytest.mark.parametrize(
    ("basis", "price_currency", "level_currency", "note"),
    [
        # Pence never equal pounds, whatever the spelling.
        ("market", "GBP", "GBp", "Strategy prices are in a different unit"),
        ("market", "GBp", "GBX", "Strategy prices are in a different unit"),
        ("market", "GBP", "USD", "Strategy prices are in a different unit"),
        ("market", "GBP", None, "Strategy prices are in a different unit"),
        ("mixed", "GBp", "GBP", "Strategy prices are in a different unit"),
        # An average-cost level is in the cost currency (GBP here).
        ("average_cost", "GBp", "GBP", "Cost and price are in different units"),
        ("average_cost", "USD", "GBP", "Cost and price are in different units"),
    ],
)
def test_unit_guards(
    basis: str, price_currency: str, level_currency: str | None, note: str
) -> None:
    position = _position(price_currency=price_currency)
    level = stop_level(currency=level_currency, basis=basis)

    s = suggest_stop(position, result(AAA=level))

    assert s.level is None
    assert s.note == note


@pytest.mark.parametrize(
    ("basis", "price_currency", "cost_currency", "level_currency"),
    [
        ("market", "GBp", "GBp", "GBp"),
        # A market level needs only the price unit: cost may differ.
        ("market", "GBp", "GBP", "GBp"),
        ("market", "USD", "GBP", "USD"),
        ("average_cost", "USD", "USD", "USD"),
        ("mixed", "GBp", "GBp", "gbp"),
    ],
)
def test_matching_units_are_suggested(
    basis: str, price_currency: str, cost_currency: str, level_currency: str
) -> None:
    position = _position(price_currency=price_currency, cost_currency=cost_currency)
    level = stop_level(currency=level_currency, basis=basis)

    assert suggest_stop(position, result(AAA=level)).level == Decimal("95")


def test_view_suggests_only_for_held_holdings_without_a_recorded_stop() -> None:
    held = _position()
    stopped = _position(ticker="BBB", stop_loss=85.0)
    closed = _position(ticker="CCC", shares=0.0)
    outcome = result(AAA=stop_level(), BBB=stop_level(), CCC=stop_level())

    rows = build_agent_view(7, [held, stopped, closed], outcome, None, {}).rows

    assert [row.stop is not None for row in rows] == [True, False, False]
    # A suggestion is never evidence: the Position keeps no stop.
    assert held.stop_loss is None


def test_a_recorded_zero_stop_counts_as_recorded() -> None:
    zero = _position(stop_loss=0.0)

    [row] = build_agent_view(7, [zero], result(AAA=stop_level()), None, {}).rows

    assert row.stop is None


def test_assignment_lookup_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Failing:
        def assignment_view(self, portfolio_id: int) -> AssignmentView:
            raise RuntimeError("store unreadable")

        def freshness(self) -> str:
            return "fresh"

    svc = _make_service(monkeypatch)
    svc._assignment_service = cast(StrategyAssignmentService, _Failing())
    ctx = svc.portfolio_partial_context(
        [_position()], gbpusd_rate=2.0, cash_balance=0.0, portfolio_id=1
    )

    assert ctx["strategy_assignment"] is None
    assert any(r.exc_info for r in caplog.records)


# --- the agents partial's Stop cell --------------------------------------------


def render_stop_cell(position: Position, outcome: RecommendationOutcome) -> str:
    """Render the agents partial and return the Stop cell for ``position``."""
    view = build_agent_view(7, [position], outcome, None, {})
    html = templates.get_template("_portfolio_agents.html").render(view=view)
    marker = f'id="agent-7-stop-{position.ticker}"'
    assert marker in html
    return html.split(marker, 1)[1].split("</div>", 1)[0]


def test_stop_cell_shows_the_suggestion_with_use_action() -> None:
    cell = render_stop_cell(_position(), result(AAA=stop_level()))

    assert 'class="agent-cell stop-suggestion" data-agent-cell="7"' in cell
    assert 'hx-swap-oob="true"' in cell
    assert "Suggested" in cell
    assert "£95.00" in cell
    assert "Close below the 50-day average · -5.0%" in cell
    assert 'title="Close that breaks the 50-session SMA: 95"' in cell
    assert 'hx-post="/portfolios/7/positions/AAA/stop"' in cell
    assert """hx-vals='{"stop_loss": "95"}'""" in cell
    assert 'hx-confirm="Record a stop of £95.00 on your latest AAA buy?"' in cell
    assert 'hx-target="#tab-content"' in cell
    assert 'class="neg"' not in cell
    assert "stale scan" not in cell


def test_stop_cell_shows_a_stale_level_without_use() -> None:
    cell = render_stop_cell(_position(), result("stale", AAA=stop_level()))

    assert "£95.00" in cell
    assert "Based on a stale scan (as of 25 Sep 2026)" in cell
    assert "/stop" not in cell


def test_stop_cell_uses_the_row_symbol_flag_and_note() -> None:
    usd = _position(price_currency="USD", cost_currency="USD", current_price=90.0)
    level = stop_level(currency="USD", note="Not part of its rules.")

    cell = render_stop_cell(usd, result(AAA=level))

    assert "$95.00" in cell
    assert "+5.6%" in cell
    assert "Price below the suggested stop" in cell
    assert "Not part of its rules." in cell


def test_stop_cell_declares_a_reason_without_level_or_use() -> None:
    cell = render_stop_cell(_position(), NO_ASSIGNMENT)

    assert "No suggestion — No Strategy assigned" in cell
    assert "Suggested" not in cell
    assert "/stop" not in cell


def test_small_level_shows_four_places_but_posts_full_precision() -> None:
    level = "0.0345000000000001"

    cell = render_stop_cell(
        _position(current_price=0.04), result(AAA=stop_level(level))
    )

    assert "£0.0345" in cell
    assert f"""hx-vals='{{"stop_loss": "{level}"}}'""" in cell
    assert 'hx-confirm="Record a stop of £0.0345 on your latest AAA buy?"' in cell
