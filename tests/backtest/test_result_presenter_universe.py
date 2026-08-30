"""Unit tests for the shared universe view-model builder (gh-434).

Covers every I/O & Edge-Case Matrix row for ``build_universe_view``:
multi-security count labels, single-ticker labels, the whole-universe
claim, legacy runs without a selection, unresolvable security IDs, and
missing-snapshot degradation.
"""

from __future__ import annotations

from app.services.backtest.result_presenter import (
    NO_UNIVERSE_LABEL,
    SELECTED_SECURITIES_MODE,
    UNRESOLVED_SECURITY_LABEL,
    WHOLE_UNIVERSE_MODE,
    build_universe_view,
)

_IDENTITIES = {
    "sid_002": ("MSFT", "XNAS"),
    "sid_001": ("AAPL", "XNYS"),
    "sid_003": ("BARC", "XLON"),
}


def test_multi_security_run_labels_count_and_lists_sorted_tickers() -> None:
    view = build_universe_view(
        ("sid_002", "sid_001"),
        _IDENTITIES,
        runnable_ids=("sid_001", "sid_002", "sid_003"),
    )

    assert view.label == "2 securities"
    assert view.tickers == ("AAPL (XNYS)", "MSFT (XNAS)")
    assert view.roster_count == 3
    assert view.runnable_count == 3
    assert view.excluded_count == 0
    assert view.selection_mode == SELECTED_SECURITIES_MODE


def test_single_security_run_labels_the_ticker_itself() -> None:
    view = build_universe_view(
        ("sid_001",), _IDENTITIES, runnable_ids=("sid_001", "sid_002", "sid_003")
    )

    assert view.label == "AAPL (XNYS)"
    assert view.tickers == ("AAPL (XNYS)",)
    assert view.selection_mode == SELECTED_SECURITIES_MODE


def test_selection_equal_to_the_runnable_set_claims_whole_universe() -> None:
    view = build_universe_view(
        ("sid_001", "sid_002", "sid_003"),
        _IDENTITIES,
        runnable_ids=("sid_003", "sid_001", "sid_002"),
    )

    assert view.label == "Whole universe (3)"
    assert view.roster_count == 3
    assert view.runnable_count == 3
    assert view.excluded_count == 0
    assert view.selection_mode == WHOLE_UNIVERSE_MODE


def test_selection_matching_only_the_runnable_count_is_not_whole_universe() -> None:
    """A hand-ticked subset that happens to match the runnable universe's
    size must not be mislabelled -- the claim requires set equality."""
    view = build_universe_view(
        ("sid_001", "sid_002"),
        _IDENTITIES,
        runnable_ids=("sid_002", "sid_003"),
    )

    assert view.label == "2 securities"
    assert view.selection_mode == SELECTED_SECURITIES_MODE


def test_whole_universe_excluded_count_reflects_roster_minus_runnable() -> None:
    identities = {**_IDENTITIES, "sid_004": ("TSLA", "XNAS")}
    view = build_universe_view(
        ("sid_001", "sid_002", "sid_003"),
        identities,
        runnable_ids=("sid_001", "sid_002", "sid_003"),
    )

    assert view.label == "Whole universe (3)"
    assert view.roster_count == 4
    assert view.excluded_count == 1


def test_excluded_count_never_going_negative_when_roster_shrinks() -> None:
    """A roster rewritten smaller than the pinned snapshot must not render
    a negative 'Excluded' figure -- it degrades to None instead."""
    view = build_universe_view(
        ("sid_001", "sid_002"),
        {"sid_001": ("AAPL", "XNYS")},
        runnable_ids=("sid_001", "sid_002"),
    )

    assert view.runnable_count == 2
    assert view.excluded_count is None


def test_legacy_run_without_selection_shows_placeholder() -> None:
    view = build_universe_view(None, _IDENTITIES, runnable_ids=("sid_001",))

    assert view.label == NO_UNIVERSE_LABEL
    assert view.tickers == ()
    assert view.roster_count is None
    assert view.runnable_count is None
    assert view.excluded_count is None
    assert view.selection_mode is None


def test_unresolvable_security_id_shows_unknown_security() -> None:
    view = build_universe_view(("sid_001", "ghost-id"), _IDENTITIES)

    assert UNRESOLVED_SECURITY_LABEL in view.tickers
    assert "ghost-id" not in view.tickers
    assert view.label == "2 securities"


def test_missing_snapshot_month_degrades_to_count_only() -> None:
    view = build_universe_view(("sid_001", "sid_002"), _IDENTITIES, runnable_ids=None)

    assert view.label == "2 securities"
    assert view.runnable_count is None
    assert view.excluded_count is None
    assert view.selection_mode == SELECTED_SECURITIES_MODE


def test_empty_roster_never_raises_and_marks_every_id_unresolved() -> None:
    view = build_universe_view(("sid_001", "sid_002"), {})

    assert view.tickers == (UNRESOLVED_SECURITY_LABEL,) * 2
    assert view.roster_count == 0
    assert view.label == "2 securities"
