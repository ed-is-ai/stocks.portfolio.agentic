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
        "chart_total_values": "[]",
        "chart_values": "[]",
        "chart_costs": "[]",
        "chart_cash": "[]",
        "chart_has_unavailable_totals": False,
        "chart_all_totals_unavailable": False,
        "chart_buys": "[]",
        "chart_sells": "[]",
        "chart_buy_tips": "[]",
        "chart_sell_tips": "[]",
        "total_cost_gbp": 0,
        "total_value_gbp": 0,
        "market_value_gbp": 0,
        "total_pnl_gbp": 0,
        "total_cost_gbp_valued": 0,
        "prices_as_of": None,
        "gbpusd_rate": None,
        "error_message": None,
        "warning_message": None,
    }
    return templates.get_template("_portfolio.html").render(**context)


def _render_with_total_pnl(total_pnl_gbp: float) -> str:
    """Render one valued position so the unrealised P&L summary is visible."""
    context = {
        "positions": [_fake_position()],
        "cash_balance": 0.0,
        "cash_flows": [],
        "positions_with_value": [_fake_position()],
        "chart_points": 0,
        "portfolio_id": None,
        "portfolios": [],
        "active_portfolio": None,
        "chart_labels": "[]",
        "chart_total_values": "[]",
        "chart_values": "[]",
        "chart_costs": "[]",
        "chart_cash": "[]",
        "chart_has_unavailable_totals": False,
        "chart_all_totals_unavailable": False,
        "chart_buys": "[]",
        "chart_sells": "[]",
        "chart_buy_tips": "[]",
        "chart_sell_tips": "[]",
        "total_cost_gbp": 100,
        "total_value_gbp": 100 + total_pnl_gbp,
        "market_value_gbp": 100 + total_pnl_gbp,
        "total_pnl_gbp": total_pnl_gbp,
        "total_cost_gbp_valued": 100,
        "prices_as_of": None,
        "gbpusd_rate": None,
        "error_message": None,
        "warning_message": None,
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


def test_unrealised_pnl_summary_uses_positive_directional_cue_and_signed_value() -> (
    None
):
    html = _render_with_total_pnl(12.5)

    assert 'class="stat-card pnl-positive"' in html
    assert 'class="sval pos"' in html
    assert "+£12.50" in html


def test_zero_unrealised_pnl_summary_uses_positive_directional_cue_and_signed_value() -> (
    None
):
    html = _render_with_total_pnl(0)

    assert 'class="stat-card pnl-positive"' in html
    assert 'class="sval pos"' in html
    assert "+£0.00" in html


def test_unrealised_pnl_summary_uses_negative_directional_cue_and_signed_value() -> (
    None
):
    html = _render_with_total_pnl(-12.5)

    assert 'class="stat-card pnl-negative"' in html
    assert 'class="sval neg"' in html
    assert "£-12.50" in html


def test_stat_card_pnl_value_colour_beats_the_base_slate_rule() -> None:
    """The `sval pos`/`sval neg` classes only tint the value if a rule more
    specific than `.stat-card .sval` (which hard-codes slate) exists. Without
    this the P&L summary renders grey despite the class (#417)."""
    from pathlib import Path

    index_css = Path("app/api/templates/index.html").read_text(encoding="utf-8")
    base = index_css.index(".stat-card .sval {")
    pos = index_css.index(".stat-card .sval.pos")
    neg = index_css.index(".stat-card .sval.neg")
    assert pos > base and neg > base
    assert "var(--green)" in index_css[pos : pos + 60]
    assert "var(--red)" in index_css[neg : neg + 60]


def test_summary_cards_have_the_required_market_first_order() -> None:
    html = _render_with_total_pnl(12.5)

    labels = ["Market Value", "Total Cost", "Unrealised P&amp;L", "Cash"]
    indices = [html.index(label) for label in labels]
    assert indices == sorted(indices)
    assert "Positions</div>" not in html[indices[0] : indices[-1]]
    assert "Includes cash." in html


def test_market_value_headline_uses_total_value_including_cash() -> None:
    html = templates.get_template("_portfolio.html").render(
        positions=[_fake_position()],
        cash_balance=50,
        cash_flows=[],
        positions_with_value=[_fake_position()],
        chart_has_history=False,
        portfolio_id=None,
        portfolios=[],
        active_portfolio=None,
        total_cost_gbp=150,
        total_value_gbp=150,
        market_value_gbp=100,
        total_pnl_gbp=0,
        total_cost_gbp_valued=100,
        prices_as_of=None,
        gbpusd_rate=None,
        error_message=None,
        warning_message=None,
    )

    market_card = html.split("Market Value", 1)[1].split("</div>", 2)[1]
    assert "£150.00" in market_card
    assert "£100.00" not in market_card


def test_dashboard_template_keeps_context_actions_and_chart_fragment_contracts() -> (
    None
):
    from pathlib import Path

    template = Path("app/api/templates/_portfolio.html").read_text(encoding="utf-8")
    chart = Path("app/api/templates/_portfolio_chart.html").read_text(encoding="utf-8")

    assert 'class="portfolio-dashboard"' in template
    assert 'aria-label="Account and strategy"' in template
    assert 'aria-label="Portfolio actions"' in template
    assert 'class="btn btn-sm btn-primary"' in template
    assert 'id="portfolioSelect"' in template
    assert 'id="refreshBtn"' in template
    assert 'hx-get="/portfolios/{{ active_portfolio.id }}/recommendations"' in template
    assert 'id="portfolio-chart-card"' in chart
    assert 'hx-target="#portfolio-chart-card" hx-swap="outerHTML"' in chart
    assert template.index('{% include "_portfolio_chart.html" %}') < template.index(
        'class="portfolio-summary-grid"'
    )


def test_chart_makes_portfolio_value_dominant_and_supporting_lines_distinct() -> None:
    html = templates.get_template("_portfolio_chart.html").render(
        portfolio_id=1,
        chart_range="12M",
        chart_points=2,
        chart_labels='["2026-08-01", "2026-08-02"]',
        chart_total_values="[110, 125]",
        chart_values="[100, 120]",
        chart_costs="[90, 90]",
        chart_cash="[10, 5]",
        chart_has_unavailable_totals=False,
        chart_all_totals_unavailable=False,
        chart_buys="[null, 120]",
        chart_sells="[null, null]",
        chart_buy_tips='[null, "BUY 1 AAPL"]',
        chart_sell_tips="[null, null]",
    )

    labels = ["Portfolio Value", "Market Value", "Cost Basis", "Cash"]
    assert [html.index(f"label: '{label}'") for label in labels] == sorted(
        html.index(f"label: '{label}'") for label in labels
    )
    portfolio_dataset = html[html.index("label: 'Portfolio Value'") :]
    market_dataset = html[html.index("label: 'Market Value'") :]
    cost_dataset = html[html.index("label: 'Cost Basis'") :]
    cash_dataset = html[html.index("label: 'Cash'") :]
    assert "borderWidth: 3" in portfolio_dataset[:400]
    assert "borderDash" not in portfolio_dataset[:400]
    assert "borderDash: [8, 4]" in market_dataset[:400]
    assert "borderDash: [2, 3]" in cost_dataset[:400]
    assert "borderDash: [10, 4, 2, 4]" in cash_dataset[:400]
    assert "hidden: true" in market_dataset[:400]
    assert "hidden: true" in cost_dataset[:400]
    assert "hidden: true" in cash_dataset[:400]
    assert "maintainAspectRatio: false" in html
    assert "position: 'bottom'" in html
    assert 'role="img"' in html
    assert 'aria-label="Portfolio value history' in html
    assert "Portfolio value history chart." in html


def test_chart_reports_unavailable_totals_without_hiding_supporting_series() -> None:
    html = templates.get_template("_portfolio_chart.html").render(
        portfolio_id=1,
        chart_range="12M",
        chart_points=2,
        chart_labels='["2026-08-01", "2026-08-02"]',
        chart_total_values="[110, null]",
        chart_values="[100, 120]",
        chart_costs="[90, 90]",
        chart_cash="[10, null]",
        chart_has_unavailable_totals=True,
        chart_all_totals_unavailable=False,
        chart_buys="[null, null]",
        chart_sells="[null, null]",
        chart_buy_tips="[null, null]",
        chart_sell_tips="[null, null]",
    )

    assert "Some Portfolio Value points are unavailable" in html
    assert "const totals" in html and "[110, null]" in html
    assert "label: 'Market Value'" in html
    assert "label: 'Cost Basis'" in html
    assert "label: 'Cash'" in html
    assert "window.__portfolioChart?.destroy();" in html


def test_chart_distinguishes_all_totals_unavailable_from_partial_history() -> None:
    html = templates.get_template("_portfolio_chart.html").render(
        portfolio_id=1,
        chart_range="12M",
        chart_points=2,
        chart_labels='["2026-08-01", "2026-08-02"]',
        chart_total_values="[null, null]",
        chart_values="[100, 120]",
        chart_costs="[90, 90]",
        chart_cash="[null, null]",
        chart_has_unavailable_totals=True,
        chart_all_totals_unavailable=True,
        chart_buys="[null, null]",
        chart_sells="[null, null]",
        chart_buy_tips="[null, null]",
        chart_sell_tips="[null, null]",
    )

    assert "Portfolio Value is unavailable because" in html
    assert "Some Portfolio Value points are unavailable" not in html


def test_chart_empty_state_preserves_selector_and_tears_down_instance() -> None:
    html = templates.get_template("_portfolio_chart.html").render(
        portfolio_id=1,
        chart_range="1M",
        chart_points=1,
        chart_has_unavailable_totals=False,
        chart_all_totals_unavailable=False,
    )

    assert "No data in this range" in html
    assert 'aria-label="Chart time range"' in html
    assert "window.__portfolioChart.destroy()" in html
    assert "window.__portfolioChart=null" in html


def test_dashboard_empty_state_does_not_emit_an_orphan_section_closer() -> None:
    html = templates.get_template("_portfolio.html").render(no_portfolios=True)

    assert html.count("<section") == html.count("</section>")


def test_dashboard_responsive_styles_remain_scoped_and_allow_narrow_reflow() -> None:
    from pathlib import Path

    css = Path("app/api/templates/index.html").read_text(encoding="utf-8")

    assert "repeat(auto-fit, minmax(min(100%, 10rem), 1fr))" in css
    assert "height: clamp(13rem, 20vw, 16rem)" in css
    assert "#tab-content { padding: 1rem; }" not in css
    assert "calc(100vw - 2rem)" in css


def test_zero_cash_is_visible_for_cash_only_portfolio() -> None:
    html = _render(0.0)
    row = _cash_row(html)

    assert row is not None
    assert "<td>£0.00</td>" in row
    assert "No open positions yet" not in html


def test_oversold_position_does_not_prefill_adjust_or_sell_with_negative_shares() -> (
    None
):
    """Story 2.3: an oversold position's ``shares`` can be negative -- the
    Adjust/Sell modal shares field (``min="0.0001" required``) must never be
    pre-filled with it, or the browser blocks submission outright. The
    template must substitute ``0`` (rendered as an empty field by
    ``openAdjust``/``openSell``'s ``shares || ''``) instead."""
    oversold = _fake_position()
    oversold.shares = -15.0
    html = _render(0.0, positions=[oversold])

    assert "openAdjust('AAPL', -15.0" not in html
    assert "openSell('AAPL', -15.0" not in html
    assert "openAdjust('AAPL', 0," in html
    assert "openSell('AAPL', 0," in html


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


def test_partial_refresh_warning_is_visible() -> None:
    context = {
        "positions": [],
        "cash_balance": None,
        "cash_flows": [],
        "positions_with_value": [],
        "chart_points": 0,
        "portfolio_id": None,
        "portfolios": [],
        "active_portfolio": None,
        "chart_labels": "[]",
        "chart_total_values": "[]",
        "chart_values": "[]",
        "chart_costs": "[]",
        "chart_cash": "[]",
        "chart_has_unavailable_totals": False,
        "chart_all_totals_unavailable": False,
        "chart_buys": "[]",
        "chart_sells": "[]",
        "chart_buy_tips": "[]",
        "chart_sell_tips": "[]",
        "total_cost_gbp": 0,
        "total_value_gbp": 0,
        "market_value_gbp": 0,
        "total_pnl_gbp": 0,
        "total_cost_gbp_valued": 0,
        "prices_as_of": None,
        "gbpusd_rate": None,
        "error_message": None,
        "warning_message": "Prices refreshed partially; using cached values for: BAD",
    }
    html = templates.get_template("_portfolio.html").render(**context)
    assert "alert-warning" in html
    assert "using cached values for: BAD" in html
