"""Persisted initial-basket Result projections for GH-371."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.backtest.result_presenter import initial_basket_view
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
