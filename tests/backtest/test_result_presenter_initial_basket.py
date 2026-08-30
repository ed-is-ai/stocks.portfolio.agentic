"""Persisted initial-basket Result projections for GH-371."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.backtest.metrics import MetricAvailabilityV1
from app.services.backtest.result_presenter import (
    backtest_metrics_view,
    initial_basket_view,
    result_financials_view,
)
from app.services.backtest.strategy_protocol import (
    EntrySelectionDecisionV1,
    EntrySelectionState,
    InitialEntrySelectionV1,
    Signal,
    SignalSide,
)


def _selection(*decisions: EntrySelectionDecisionV1) -> InitialEntrySelectionV1:
    return InitialEntrySelectionV1(
        session=date(2026, 6, 1),
        metric_id="split_adjusted_close_return_252_sessions",
        metric_version="v1",
        rule_id="buy_and_hold_top_x_entry_v1",
        decisions=decisions,
        signals=(
            Signal(
                security_id="selected",
                side=SignalSide.BUY,
                session=date(2026, 6, 1),
                rule_id="buy_and_hold_top_x_entry_v1",
            ),
        ),
    )


def test_initial_basket_uses_stored_rank_decimal_and_plain_exclusion() -> None:
    result = SimpleNamespace(
        initial_entry_selection=_selection(
            EntrySelectionDecisionV1(
                security_id="excluded",
                rank=3,
                state=EntrySelectionState.EXCLUDED,
                reason_code="insufficient_history",
            ),
            EntrySelectionDecisionV1(
                security_id="selected",
                rank=1,
                state=EntrySelectionState.SELECTED,
                score=Decimal("0.1264"),
            ),
            EntrySelectionDecisionV1(
                security_id="not-selected",
                rank=2,
                state=EntrySelectionState.ELIGIBLE_NOT_SELECTED,
                score=Decimal("-0.001"),
            ),
        )
    )

    view = initial_basket_view(
        result,
        {"selected": ("AAPL", "XNAS"), "not-selected": ("MSFT", "XNAS")},
    )

    assert view.recorded is True
    assert view.selection_session == "2026-06-01"
    assert (view.metric_id, view.metric_version) == (
        "split_adjusted_close_return_252_sessions",
        "v1",
    )
    assert [row.security_id for row in view.rows] == [
        "selected",
        "not-selected",
        "excluded",
    ]
    assert [row.trailing_return for row in view.rows] == ["12.6%", "-0.1%", "—"]
    assert view.rows[0].security_label == "AAPL (XNAS)"
    assert view.rows[2].security_label == "Unknown security"
    assert view.rows[2].exclusion == (
        "Insufficient price history for the 252-session return."
    )
    assert "insufficient_history" not in view.rows[2].exclusion


def test_initial_basket_distinguishes_legacy_and_all_excluded_results() -> None:
    legacy = initial_basket_view(SimpleNamespace(initial_entry_selection=None), {})
    assert legacy.recorded is False
    assert legacy.rows == ()

    result = SimpleNamespace(
        initial_entry_selection=_selection(
            EntrySelectionDecisionV1(
                security_id="short-history",
                rank=1,
                state=EntrySelectionState.EXCLUDED,
                reason_code="insufficient_history",
            )
        )
    )
    all_excluded = initial_basket_view(result, {})
    assert all_excluded.recorded is True
    assert all_excluded.has_selected is False
    assert all_excluded.rows[0].outcome == "Excluded"


def test_initial_basket_formats_large_persisted_decimal_scores() -> None:
    result = SimpleNamespace(
        initial_entry_selection=_selection(
            EntrySelectionDecisionV1(
                security_id="selected",
                rank=1,
                state=EntrySelectionState.SELECTED,
                score=Decimal("1e100"),
            )
        )
    )

    assert initial_basket_view(result, {}).rows[0].trailing_return == (
        "1" + "0" * 102 + ".0%"
    )


def test_backtest_metric_display_uses_consistent_precision_and_tones() -> None:
    metrics = SimpleNamespace(
        total_return=0.1254,
        sharpe_ratio=1.234,
        win_rate=0.5,
        max_drawdown=-0.0354,
    )

    view = backtest_metrics_view(metrics, MetricAvailabilityV1())

    assert view.total_return.value == "+12.5%"
    assert view.total_return.css_class == "pos"
    assert view.win_rate.value == "50.0%"
    assert view.win_rate.css_class == ""
    assert view.max_drawdown.value == "-3.5%"
    assert view.max_drawdown.css_class == "neg"


def test_backtest_metric_display_keeps_zero_neutral_and_nulls_unavailable() -> None:
    metrics = SimpleNamespace(
        total_return=0.0,
        sharpe_ratio=None,
        win_rate=None,
        max_drawdown=0.0,
    )
    availability = MetricAvailabilityV1()

    view = backtest_metrics_view(metrics, availability)

    assert view.total_return.value == "0.0%"
    assert view.total_return.css_class == ""
    assert view.max_drawdown.value == "0.0%"
    assert view.max_drawdown.css_class == ""
    assert view.win_rate.value == "Not applicable — no closed trades"


def test_result_financials_derive_display_only_pnl_and_currency_conventions() -> None:
    result = SimpleNamespace(
        base_currency="GBP",
        starting_capital=Decimal("10000"),
        equity_curve=(SimpleNamespace(total_equity_base=Decimal("11234.567")),),
    )

    view = result_financials_view(result)

    assert view.starting_capital.value == "£10,000.00"
    assert view.pnl.value == "+£1,234.57"
    assert view.pnl.css_class == "pos"

    usd = result_financials_view(
        SimpleNamespace(
            base_currency="USD",
            starting_capital=Decimal("10000"),
            equity_curve=(SimpleNamespace(total_equity_base=Decimal("9500")),),
        )
    )
    assert usd.starting_capital.value == "10,000.00 USD"
    assert usd.pnl.value == "-500.00 USD"
    assert usd.pnl.css_class == "neg"
