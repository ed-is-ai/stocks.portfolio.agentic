"""Direct Jinja renders of the Story 2.4 template additions.

Follows ``tests/test_portfolio_template.py``'s pattern: every route test
that touches these templates monkeypatches ``TemplateResponse`` away, so
these are the only tests that exercise the templates' real conditionals
(and, incidentally, catch a Jinja syntax error the route tests can't).
"""

from __future__ import annotations

from app.api.templating import templates
from app.schemas import (
    MatchTrace,
    MatchTraceCandidateLot,
    SkippedInvalidDateTrade,
    Trade,
)

# --- _history.html -----------------------------------------------------------


def _render_history(trades: list[Trade], opening_lot_status: dict) -> str:
    return templates.get_template("_history.html").render(
        trades=trades,
        portfolio_names={1: "SIPP"},
        opening_lot_status=opening_lot_status,
    )


def _opening_lot_trade(trade_id: int = 1) -> Trade:
    return Trade(
        id=trade_id,
        ticker="AAPL",
        action="BUY",
        shares=10,
        price=100.0,
        date="2026-01-01",
        portfolio_id=1,
        source="opening_lot",
    )


def test_history_no_trades_shows_empty_state() -> None:
    html = _render_history([], {})
    assert "No trades recorded yet." in html


def test_history_opening_lot_shows_manually_entered_badge() -> None:
    html = _render_history([_opening_lot_trade()], {1: "unconsumed"})
    assert "Manually entered" in html


def test_history_unconsumed_opening_lot_shows_edit_and_delete(tmp_path=None) -> None:
    html = _render_history([_opening_lot_trade()], {1: "unconsumed"})
    assert "Edit" in html
    assert 'hx-delete="/portfolio/opening-lot/1?portfolio_id=1"' in html
    assert 'hx-post="/portfolio/opening-lot/1/edit"' in html


def test_history_consumed_opening_lot_is_read_only() -> None:
    html = _render_history([_opening_lot_trade()], {1: "consumed"})
    assert "Read-only" in html
    assert 'hx-delete="/portfolio/opening-lot/1' not in html
    assert 'hx-post="/portfolio/opening-lot/1/edit"' not in html


def test_history_regular_trade_uses_generic_delete_route() -> None:
    regular = Trade(
        id=2,
        ticker="MSFT",
        action="BUY",
        shares=5,
        price=200.0,
        date="2026-01-01",
        portfolio_id=1,
        source="manual",
    )
    html = _render_history([regular], {})
    assert "Manually entered" not in html
    assert 'hx-delete="/trades/2"' in html


def test_history_pre_migration_trade_with_null_source_renders_generic_delete() -> None:
    """A pre-Story-2.4 row (``source`` NULL) must render exactly like
    every other non-Opening-Lot trade -- no crash, no mislabel."""
    legacy = Trade(
        id=3,
        ticker="TSLA",
        action="SELL",
        shares=1,
        price=300.0,
        date="2026-01-01",
        portfolio_id=1,
        source=None,
    )
    html = _render_history([legacy], {})
    assert "Manually entered" not in html
    assert 'hx-delete="/trades/3"' in html


# --- _match_trace.html --------------------------------------------------------


def _render_match_trace(
    trace: MatchTrace | None, portfolio_id: int = 1, trade_id: int = 42
):
    return templates.get_template("_match_trace.html").render(
        trace=trace, portfolio_id=portfolio_id, trade_id=trade_id
    )


def test_match_trace_none_shows_not_found_message() -> None:
    html = _render_match_trace(None)
    assert "No match trace found for trade #42" in html


def test_match_trace_renders_full_content() -> None:
    trace = MatchTrace(
        trade_id=42,
        ticker="AAPL",
        portfolio_id=1,
        date="2026-02-01",
        shares=8,
        price=150.0,
        shares_matched=5,
        shares_unmatched=3,
        candidate_lots=[
            MatchTraceCandidateLot(
                trade_id=7,
                buy_date="2026-01-01",
                buy_price=100.0,
                shares_consumed=5,
                source="opening_lot",
                import_batch_id=None,
                is_opening_lot=True,
            )
        ],
        ordering_note="Chronological replay: source_row_index=None, idempotency_key=None.",
        skipped_invalid_date_trades=[
            SkippedInvalidDateTrade(
                trade_id=99,
                ticker="AAPL",
                raw_date="not-a-date",
                reason="Trade date 'not-a-date' is not valid ISO-8601",
            )
        ],
        source="sipp_import",
        import_batch_id="batch-xyz",
        reason="Sell exceeds available BUY lots by 3 shares",
    )

    html = _render_match_trace(trace)

    assert "AAPL" in html
    assert "sipp_import" in html
    assert "batch-xyz" in html
    assert "Sell exceeds available BUY lots by 3 shares" in html
    assert "Manually entered" in html  # candidate lot badge
    assert "not-a-date" in html
    assert "source_row_index" in html


def test_match_trace_fully_matched_shows_no_shortfall_reason() -> None:
    trace = MatchTrace(
        trade_id=42,
        ticker="AAPL",
        portfolio_id=1,
        date="2026-02-01",
        shares=5,
        price=150.0,
        shares_matched=5,
        shares_unmatched=0,
        candidate_lots=[],
        ordering_note="note",
        source=None,
        import_batch_id=None,
        reason=None,
    )

    html = _render_match_trace(trace)

    assert "Fully matched" in html
    assert "Unknown / pre-migration" in html  # source is None
