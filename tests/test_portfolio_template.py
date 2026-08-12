"""Direct Jinja renders of ``_portfolio.html``'s cash-balance display.

Every other portfolio route test monkeypatches ``TemplateResponse`` away, so
these are the only tests that exercise the template's real conditionals.
"""

from types import SimpleNamespace
from typing import Any

from app.api.templating import templates


def _render(cash_balance: float | None, positions: list[Any] | None = None) -> str:
    context = {
        "positions": [] if positions is None else positions,
        "cash_balance": cash_balance,
        "cash_flows": [],
        "positions_with_value": [],
        "chart_points": 0,
        "portfolio_id": None,
        "portfolios": [],
        "active_portfolio": None,
        "chart_labels": "[]",
        "chart_values": "[]",
        "chart_costs": "[]",
        "chart_cash": "[]",
        "chart_buys": "[]",
        "chart_sells": "[]",
        "chart_buy_tips": "[]",
        "chart_sell_tips": "[]",
        "total_cost_gbp": 0,
        "total_value_gbp": 0,
        "total_pnl_gbp": 0,
        "total_cost_gbp_valued": 0,
        "prices_as_of": None,
        "gbpusd_rate": None,
        "error_message": None,
    }
    return templates.get_template("_portfolio.html").render(**context)


def _cash_row(html: str) -> str | None:
    """Return the CASH table row, or None when it isn't rendered.

    Value assertions must target this row specifically. The summary tiles
    and the row's own hardcoded P&L cell both emit ``£0.00``, so a bare
    ``"£0.00" in html`` passes for any balance and proves nothing.
    """
    for row in html.split("<tr>"):
        if ">CASH<" in row:
            return row
    return None


def _fake_position() -> SimpleNamespace:
    return SimpleNamespace(
        ticker="AAPL",
        shares=1,
        avg_cost=100,
        price_currency="GBP",
        total_cost=100,
        current_price=100,
        current_value=100,
        unrealised_pnl=0,
        unrealised_pnl_pct=0,
        entry_price=None,
        entry_date=None,
        stop_loss=None,
        profit_target_20=None,
        profit_target_25=None,
        next_pivot=None,
        exit_signal=None,
    )


def test_zero_cash_is_visible_with_positions() -> None:
    row = _cash_row(_render(0.0, positions=[_fake_position()]))

    assert row is not None
    assert "<td>£0.00</td>" in row


def test_zero_cash_is_visible_for_cash_only_portfolio() -> None:
    html = _render(0.0)
    row = _cash_row(html)

    assert row is not None
    assert "<td>£0.00</td>" in row
    assert "No open positions yet" not in html


def test_negative_cash_is_not_hidden() -> None:
    row = _cash_row(_render(-150.5))

    assert row is not None
    assert "<td>£-150.50</td>" in row


def test_missing_cash_still_shows_empty_state() -> None:
    html = _render(None)

    assert _cash_row(html) is None
    assert "No open positions yet" in html


def test_zero_cash_row_is_distinguishable_from_a_nonzero_balance() -> None:
    """Guards the assertions above against the row's hardcoded £0.00 P&L cell.

    A £5.00 balance still renders "£0.00" somewhere in its CASH row, so an
    unanchored substring check would pass here too.
    """
    row = _cash_row(_render(5.0))

    assert row is not None
    assert "£0.00" in row  # the hardcoded P&L cell
    assert "<td>£0.00</td>" not in row  # but not the balance cell
    assert "<td>£5.00</td>" in row
