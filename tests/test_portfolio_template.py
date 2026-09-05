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
        "chart_usable_total_points": 0,
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
        "chart_usable_total_points": 0,
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
        display_symbol="AAPL",
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
        chart_points=3,
        chart_usable_total_points=3,
        chart_labels='["2026-08-01", "2026-08-02", "2026-08-03"]',
        chart_total_values="[110, 125, 130]",
        chart_values="[100, 120, 115]",
        chart_costs="[90, 90, 90]",
        chart_cash="[10, 5, 8]",
        chart_has_unavailable_totals=False,
        chart_all_totals_unavailable=False,
        chart_buys="[null, 120, null]",
        chart_sells="[null, null, null]",
        chart_buy_tips='[null, "BUY 1 AAPL", null]',
        chart_sell_tips="[null, null, null]",
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
    # Every line series spans a null (no-evidence) point with a smoothed
    # curve rather than breaking the line there.
    assert "spanGaps: true" in portfolio_dataset[:400]
    assert "spanGaps: true" in market_dataset[:400]
    assert "spanGaps: true" in cost_dataset[:400]
    assert "spanGaps: true" in cash_dataset[:400]
    assert "maintainAspectRatio: false" in html
    assert "position: 'bottom'" in html
    assert 'role="img"' in html
    assert 'aria-label="Portfolio value history' in html
    assert "Portfolio value history chart." in html


def test_chart_range_buttons_disable_when_availability_says_no(  # noqa: E501
) -> None:
    """A preset marked unavailable (#498) renders disabled with no hx-get,
    so clicking it can't fetch a range that would look identical to 1M;
    the currently-active preset and any range not covered by the map at
    all stay clickable."""
    html = templates.get_template("_portfolio_chart.html").render(
        portfolio_id=1,
        chart_range="1M",
        chart_points=3,
        chart_usable_total_points=3,
        chart_labels='["2026-08-01", "2026-08-02", "2026-08-03"]',
        chart_total_values="[110, 125, 130]",
        chart_values="[100, 120, 115]",
        chart_costs="[90, 90, 90]",
        chart_cash="[10, 5, 8]",
        chart_has_unavailable_totals=False,
        chart_all_totals_unavailable=False,
        chart_buys="[null, null, null]",
        chart_sells="[null, null, null]",
        chart_buy_tips="[null, null, null]",
        chart_sell_tips="[null, null, null]",
        chart_range_availability={
            "1M": True,
            "3M": False,
            "12M": False,
            "3Y": False,
            "5Y": False,
        },
    )

    def button(preset: str) -> str:
        start = html.index(f"{preset}\n      </button>")
        return html[max(0, start - 400) : start]

    assert "disabled" not in button("1M")
    assert "hx-get" in button("1M")
    for preset in ("3M", "12M", "3Y", "5Y"):
        assert "disabled" in button(preset)
        assert "hx-get" not in button(preset)


def test_chart_reports_unavailable_totals_without_hiding_supporting_series() -> None:
    html = templates.get_template("_portfolio_chart.html").render(
        portfolio_id=1,
        chart_range="12M",
        chart_points=4,
        chart_usable_total_points=3,
        chart_labels='["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]',
        chart_total_values="[110, null, 120, 130]",
        chart_values="[100, 120, 110, 120]",
        chart_costs="[90, 90, 90, 90]",
        chart_cash="[10, null, 10, 10]",
        chart_has_unavailable_totals=True,
        chart_all_totals_unavailable=False,
        chart_buys="[null, null]",
        chart_sells="[null, null]",
        chart_buy_tips="[null, null]",
        chart_sell_tips="[null, null]",
    )

    assert "Some Portfolio Value points are unavailable" in html
    assert "const totals" in html and "[110, null, 120, 130]" in html
    assert "label: 'Market Value'" in html
    assert "label: 'Cost Basis'" in html
    assert "label: 'Cash'" in html
    assert "window.__portfolioChart?.destroy();" in html


def test_chart_distinguishes_all_totals_unavailable_from_partial_history() -> None:
    html = templates.get_template("_portfolio_chart.html").render(
        portfolio_id=1,
        chart_range="12M",
        chart_points=2,
        chart_usable_total_points=0,
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
        chart_usable_total_points=0,
        chart_has_unavailable_totals=False,
        chart_all_totals_unavailable=False,
    )

    assert "No data in this range" in html
    assert 'aria-label="Chart time range"' in html
    assert "window.__portfolioChart.destroy()" in html
    assert "window.__portfolioChart=null" in html


def test_chart_sparse_history_collapses_to_building_state() -> None:
    """GH-484: snapshots exist but <3 carry a usable total — no full-size
    canvas, just the compact building message, with the selector intact."""
    html = templates.get_template("_portfolio_chart.html").render(
        portfolio_id=1,
        chart_range="12M",
        chart_points=2,
        chart_usable_total_points=2,
        chart_labels='["2026-08-01", "2026-08-02"]',
        chart_total_values="[110, 125]",
        chart_values="[100, 120]",
        chart_costs="[90, 90]",
        chart_cash="[10, 5]",
        chart_has_unavailable_totals=False,
        chart_all_totals_unavailable=False,
        chart_buys="[null, null]",
        chart_sells="[null, null]",
        chart_buy_tips="[null, null]",
        chart_sell_tips="[null, null]",
    )

    assert "Building portfolio value history" in html
    assert "2 of 2 snapshots valued" in html
    assert '<canvas id="portfolioChart"' not in html
    assert 'class="portfolio-chart-canvas"' not in html
    assert 'aria-label="Chart time range"' in html
    assert 'hx-get="/partials/portfolio/chart"' in html


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
        "chart_usable_total_points": 0,
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


# --- GH-484: currency clarity, display alias, and chart degradation --------


def _position_row(html: str, ticker: str) -> str:
    """Return the table row for ``ticker``, or None when not rendered."""
    marker = f"openSell('{ticker}'"
    for row in html.split("<tr"):
        if marker in row:
            return row
    raise AssertionError(f"no row rendered for {ticker!r}")


def test_usd_row_leads_with_native_symbol_and_shows_gbp_equivalent() -> None:
    """A USD holding with a live FX rate shows ``$`` primary plus a muted
    ``≈ £`` secondary on Mkt Value and Unreal P&L (GH-484)."""
    usd = _fake_position()
    usd.ticker = "GOOGL"
    usd.display_symbol = "GOOGL"
    usd.price_currency = "USD"
    usd.current_value = 1000.0
    usd.unrealised_pnl = 250.0
    context = {
        "positions": [usd],
        "position_gbp_values": {
            "GOOGL": {"market_value_gbp": 740.74, "unrealised_pnl_gbp": 185.19}
        },
        "cash_balance": None,
        "cash_flows": [],
        "positions_with_value": [usd],
        "chart_points": 0,
        "chart_usable_total_points": 0,
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
        "gbpusd_rate": 1.35,
        "error_message": None,
        "warning_message": None,
    }
    html = templates.get_template("_portfolio.html").render(**context)
    row = _position_row(html, "GOOGL")

    assert "$1000.00" in row
    assert "&asymp; &pound;740.74" in row
    assert "+$250.00" in row
    assert "&asymp; &pound;185.19" in row


def test_usd_row_omits_gbp_equivalent_when_fx_unavailable() -> None:
    """No usable rate means no projection — never a fabricated ``≈ £``."""
    usd = _fake_position()
    usd.ticker = "GOOGL"
    usd.display_symbol = "GOOGL"
    usd.price_currency = "USD"
    usd.current_value = 1000.0
    usd.unrealised_pnl = 250.0
    context = {
        "positions": [usd],
        "position_gbp_values": {},
        "cash_balance": None,
        "cash_flows": [],
        "positions_with_value": [usd],
        "chart_points": 0,
        "chart_usable_total_points": 0,
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
    html = templates.get_template("_portfolio.html").render(**context)
    row = _position_row(html, "GOOGL")

    assert "$1000.00" in row
    assert "&asymp;" not in row


def test_stop_and_pivot_cells_use_the_row_currency_symbol() -> None:
    """Stop/Last Pivot/Next Pivot must show ``$`` for a USD position, not a
    hardcoded ``£`` (GH-484)."""
    usd = _fake_position()
    usd.price_currency = "USD"
    usd.stop_loss = 90.0
    usd.entry_price = 95.0
    usd.next_pivot = 105.0
    html = _render(None, positions=[usd])
    row = _position_row(html, "AAPL")

    assert "$90.00" in row
    assert "$95.00" in row
    assert "$105.00" in row
    assert "£90.00" not in row


def test_aliased_fund_leads_with_display_symbol_canonical_secondary() -> None:
    """The import spelling (HSFWA) leads; the canonical id appears only as
    muted secondary text after it (mirrors the recommendations screen)."""
    aliased = _fake_position()
    aliased.ticker = "0P00013P6I.L"
    aliased.display_symbol = "HSFWA"
    html = _render(0.0, positions=[aliased])

    assert "HSFWA" in html
    assert html.index("HSFWA") < html.index("0P00013P6I.L")


def test_unaliased_ticker_is_not_echoed_twice() -> None:
    """An unaliased holding has one spelling, so nothing is echoed."""
    plain = _fake_position()
    plain.ticker = "VOD.L"
    plain.display_symbol = "VOD.L"
    html = _render(0.0, positions=[plain])

    # The ticker cell must carry exactly one spelling — the Adjust/Sell
    # onclick handlers legitimately repeat it outside the cell.
    ticker_cell = _position_row(html, "VOD.L").split("</td>")[0]
    assert ticker_cell.count("VOD.L") == 1
