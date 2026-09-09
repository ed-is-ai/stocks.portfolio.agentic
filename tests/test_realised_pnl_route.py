"""Tests for GET /partials/realised-pnl (#177).

Regression-guards AC2 against the #147/#169 empty-string ``portfolio_id``
422 bug class, and smoke-tests the no-portfolios / zero-round-trips states.
"""

from __future__ import annotations

import re
import inspect
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_realised_pnl_service, get_trader_service
from app.schemas import Portfolio, RealisedPnlSummary, RoundTrip, Trade, UnmatchedSell

client = TestClient(app)
_AUTH = {"X-Auth-Token": "s3cret"}


def test_realised_pnl_route_runs_blocking_work_in_fastapi_threadpool() -> None:
    from app.api.routes.views import partial_realised_pnl

    assert not inspect.iscoroutinefunction(partial_realised_pnl)


def _stat_card(html: str, label: str) -> str:
    # ``sval`` carries conditional pos/neg classes on the tiles that colour
    # their value, so the class attribute cannot be matched literally.
    match = re.search(
        rf'<div class="stat-card">\s*<div class="slbl">.*?{re.escape(label)}</div>'
        r'\s*<div class="sval[^"]*">(.*?)</div>\s*</div>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


@pytest.fixture
def mocked():
    mock_trader = MagicMock()
    mock_trader.list_portfolios.return_value = [
        Portfolio(id=1, name="SIPP", created_at="2024-01-01"),
    ]
    mock_realised_pnl = MagicMock()
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=0.0,
        round_trip_count=0,
    )
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_realised_pnl_service] = lambda: mock_realised_pnl
    try:
        yield mock_trader, mock_realised_pnl
    finally:
        app.dependency_overrides.clear()


def test_blank_portfolio_id_does_not_422(mocked):
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": ""})
    assert resp.status_code == 200


def test_omitted_portfolio_id_does_not_422(mocked):
    resp = client.get("/partials/realised-pnl")
    assert resp.status_code == 200


def test_unknown_portfolio_id_falls_back_to_first_portfolio(mocked):
    _, mock_realised_pnl = mocked
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "999"})
    assert resp.status_code == 200
    mock_realised_pnl.compute_summary.assert_called_once_with(1)


def test_valid_portfolio_id_used_directly(mocked):
    _, mock_realised_pnl = mocked
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})
    assert resp.status_code == 200
    mock_realised_pnl.compute_summary.assert_called_once_with(1)


def test_no_portfolios_renders_empty_state_without_calling_service(mocked):
    mock_trader, mock_realised_pnl = mocked
    mock_trader.list_portfolios.return_value = []
    resp = client.get("/partials/realised-pnl")
    assert resp.status_code == 200
    mock_realised_pnl.compute_summary.assert_not_called()


def test_zero_round_trips_shows_empty_state_copy(mocked):
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})
    assert resp.status_code == 200
    assert "No Round-trips yet for this account." in resp.text
    assert _stat_card(resp.text, "Wins").strip() == "0"
    assert _stat_card(resp.text, "Losses").strip() == "0"
    assert "Avg Win % / Avg Loss %" not in resp.text
    win_card = _stat_card(resp.text, "Avg Win %")
    loss_card = _stat_card(resp.text, "Avg Loss %")
    assert win_card.count("&mdash;") == 1
    assert loss_card.count("&mdash;") == 1


def test_strip_order_and_dropped_gross_tiles(mocked):
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})
    assert resp.status_code == 200
    labels = [
        re.sub(r"<[^>]+>", "", raw).replace("&amp;", "&").strip()
        for raw in re.findall(r'<div class="slbl">(.*?)</div>', resp.text, re.DOTALL)
    ]
    strip = [
        label
        for label in labels
        if label
        in {
            "Unmatched Sells",
            "Round-trips",
            "Wins",
            "Losses",
            "Avg Win %",
            "Avg Loss %",
            "Total Realised P&L",
        }
    ]
    assert strip == [
        "Unmatched Sells",
        "Round-trips",
        "Wins",
        "Losses",
        "Avg Win %",
        "Avg Loss %",
        "Total Realised P&L",
    ]
    assert "Gross Won" not in resp.text
    assert "Gross Lost" not in resp.text


def test_index_html_has_table_cell_pnl_colour_rules():
    from pathlib import Path

    css = Path("app/api/templates/index.html").read_text()
    assert ".table tbody td.pos { color: var(--green); }" in css
    assert ".table tbody td.neg { color: var(--red); }" in css


def test_pnl_table_js_defaults():
    from pathlib import Path

    src = Path("app/api/static/js/pnl-table.js").read_text()
    assert "pnl-table-state-v1" in src
    assert "wl-state-v1" not in src
    hidden = re.search(r"DEFAULT_HIDDEN\s*=\s*\[([^\]]*)\]", src)
    assert hidden is not None
    assert "buyprice" in hidden.group(1)
    assert "sellprice" in hidden.group(1)


def test_split_average_tiles_render_populated_values(mocked):
    _, mock_realised_pnl = mocked
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=5.0,
        round_trip_count=2,
        winning_round_trip_count=1,
        losing_round_trip_count=1,
        average_win_pct=12.5,
        average_loss_pct=-4.2,
    )
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})
    assert resp.status_code == 200
    assert "+12.5%" in _stat_card(resp.text, "Avg Win %")
    assert "-4.2%" in _stat_card(resp.text, "Avg Loss %")
    win = re.search(r'Avg Win %</div>\s*<div class="sval ([^"]*)">', resp.text)
    loss = re.search(r'Avg Loss %</div>\s*<div class="sval ([^"]*)">', resp.text)
    assert win is not None and win.group(1).strip() == "pos"
    assert loss is not None and loss.group(1).strip() == "neg"


def test_history_uses_service_owned_cached_presentation(mocked):
    mock_trader, mock_realised_pnl = mocked
    opening_lot = Trade(
        id=7,
        ticker="AAPL",
        action="BUY",
        shares=10,
        price=100.0,
        date="2026-01-01",
        portfolio_id=1,
        source="opening_lot",
    )
    regular_trade = opening_lot.model_copy(update={"id": 8, "source": "manual"})
    mock_realised_pnl.get_history_presentation.return_value = (
        [opening_lot, regular_trade],
        {7: "unconsumed"},
    )

    resp = client.get("/partials/history")

    assert resp.status_code == 200
    mock_realised_pnl.get_history_presentation.assert_called_once_with([1])
    mock_trader.get_trade_history.assert_not_called()
    mock_realised_pnl.opening_lot_status.assert_not_called()


def _round_trip(
    ticker: str,
    pnl: float,
    *,
    exit_date: str = "2026-02-01",
    fx_unavailable: bool = False,
) -> RoundTrip:
    return RoundTrip(
        ticker=ticker,
        portfolio_id=1,
        entry_date="2026-01-01",
        entry_price=100.0,
        exit_date=exit_date,
        exit_price=110.0,
        shares=1.0,
        holding_period_days=31,
        realised_pnl_gbp=pnl,
        realised_pnl_pct=pnl,
        fx_unavailable=fx_unavailable,
    )


def test_round_trip_rows_are_flat_with_prices_in_the_result_tooltip(mocked):
    """The table is one row per round trip, newest exit first (#573).

    Replaces the per-ticker subtotal rows and collapsible detail tables:
    those answered "how did this ticker do", which the chart's ticker filter
    now answers better, and they buried individual trades behind a click.
    Entry and exit prices moved into the Result cell's tooltip.
    """
    _mock_trader, mock_realised_pnl = mocked
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={
            "WIN": [_round_trip("WIN", 10.0, exit_date="2026-02-02")],
            "LOSS": [_round_trip("LOSS", -5.0, exit_date="2026-02-01")],
            "USDX": [_round_trip("USDX", 0.0, fx_unavailable=True)],
        },
        total_realised_pnl_gbp=5.0,
        round_trip_count=3,
    )

    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})

    assert resp.status_code == 200
    # Every header now carries a .wl-sort button with a data-col.
    sort_cols = re.findall(r'<button class="wl-sort" data-col="([a-z]+)"', resp.text)
    assert sort_cols == [
        "ticker",
        "entered",
        "exited",
        "held",
        "stake",
        "buyprice",
        "sellprice",
        "result",
        "pnlpct",
    ]
    table = resp.text[resp.text.index('id="pnl-table"') : resp.text.index("</table>")]
    assert "subtotal" not in table
    assert "ticker-detail" not in table
    assert "<details" not in table
    # Newest exit first, across tickers rather than within one.
    assert table.index("2026-02-02") < table.index("2026-02-01")
    # Prices are reference detail, reachable but not a column to scan.
    assert "entry 100.00 &rarr; exit 110.00" in table
    # Buy/Sell price exist as hidden columns.
    assert 'class="wl-hidden" data-col="buyprice"' in table
    assert 'class="wl-hidden" data-col="sellprice"' in table
    # An unconvertible round trip says so rather than showing a figure, and
    # emits empty data-val on result/pnlpct so it sorts last.
    assert "FX rate unavailable" in table
    assert '<td data-col="result" data-val="">' in table
    assert '<td data-col="pnlpct" data-val="">' in table
    # Losing row carries neg on both Result and P&L % cells.
    loss_row = table[table.index("LOSS") : table.index("USDX")]
    assert loss_row.count('class="neg"') == 2
    win_row = table[table.index("WIN") : table.index("LOSS")]
    assert win_row.count('class="pos"') == 2


def test_toolbar_exposes_search_and_columns_mount(mocked):
    _, mock_realised_pnl = mocked
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={"WIN": [_round_trip("WIN", 10.0)]},
        total_realised_pnl_gbp=10.0,
        round_trip_count=1,
    )
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})
    assert resp.status_code == 200
    assert 'id="pnl-search"' in resp.text
    assert 'type="search"' in resp.text
    assert 'id="pnl-match-count"' in resp.text
    assert 'id="pnl-adv"' in resp.text


def test_toolbar_absent_when_no_round_trips(mocked):
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})
    assert resp.status_code == 200
    assert 'id="pnl-search"' not in resp.text


def test_currency_symbol_on_stake_and_price_cells(mocked):
    _mock_trader, mock_realised_pnl = mocked
    trip = _round_trip("USDX", 10.0).model_copy(update={"currency": "USD"})
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={"USDX": [trip]},
        total_realised_pnl_gbp=10.0,
        round_trip_count=1,
    )
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})
    assert resp.status_code == 200
    table = resp.text[resp.text.index('id="pnl-table"') : resp.text.index("</table>")]
    assert '<td data-col="stake" data-val="100.0000">$100.00</td>' in table
    assert ">$100.00</td>" in table  # buy price
    assert ">$110.00</td>" in table  # sell price


# --- Story 1.5: POST /trades/{trade_id}/ack --------------------------------


@pytest.fixture
def mocked_with_token(mocked, monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    return mocked


def _unmatched(trade_id: int, acknowledged_at: str | None) -> UnmatchedSell:
    return UnmatchedSell(
        trade_id=trade_id,
        ticker="AAPL",
        portfolio_id=1,
        date="2026-01-01",
        shares=2,
        price=100.0,
        reason="No prior BUY found to match this sell",
        acknowledged_at=acknowledged_at,
    )


def test_ack_route_returns_only_unmatched_sells_fragment(mocked_with_token):
    _, mock_realised_pnl = mocked_with_token
    mock_realised_pnl.toggle_unmatched_sell_ack.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=0.0,
        round_trip_count=0,
        unmatched_sells=[_unmatched(5, "2026-08-09T12:00:00+00:00")],
    )

    resp = client.post("/trades/5/ack", params={"portfolio_id": "1"}, headers=_AUTH)

    assert resp.status_code == 200
    assert 'id="unmatched-sells-panel"' in resp.text
    # Never a full-tab re-render (AD-8/AC #5): the summary strip is absent.
    assert "stat-card" not in resp.text
    mock_realised_pnl.toggle_unmatched_sell_ack.assert_called_once_with(5, 1)


def test_ack_route_requires_auth(mocked_with_token):
    resp = client.post(
        "/trades/5/ack",
        params={"portfolio_id": "1"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


def test_ack_route_stale_trade_id_is_noop_not_error(mocked_with_token):
    """A trade_id no longer among the account's unmatched sells (already
    resolved, or a stale/tampered id) is a no-op at the service layer --
    the route must still return 200 with the unchanged fragment, never a
    404/500."""
    _, mock_realised_pnl = mocked_with_token
    mock_realised_pnl.toggle_unmatched_sell_ack.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=0.0,
        round_trip_count=0,
        unmatched_sells=[_unmatched(5, None)],
    )

    resp = client.post("/trades/999/ack", params={"portfolio_id": "1"}, headers=_AUTH)

    assert resp.status_code == 200
    assert 'id="unmatched-sells-panel"' in resp.text
    mock_realised_pnl.toggle_unmatched_sell_ack.assert_called_once_with(999, 1)


def test_ack_route_falls_back_to_first_portfolio_when_unknown(mocked_with_token):
    _, mock_realised_pnl = mocked_with_token
    mock_realised_pnl.toggle_unmatched_sell_ack.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=0.0,
        round_trip_count=0,
    )

    resp = client.post("/trades/5/ack", params={"portfolio_id": "999"}, headers=_AUTH)

    assert resp.status_code == 200
    mock_realised_pnl.toggle_unmatched_sell_ack.assert_called_once_with(5, 1)


def test_unmatched_panel_hidden_when_zero_unmatched_sells(mocked):
    """AC #3: zero unmatched sells -> the panel div renders but is empty
    inside, no <details> at all."""
    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})
    assert resp.status_code == 200
    assert 'id="unmatched-sells-panel"' in resp.text
    assert "<details class=" not in resp.text


def test_unmatched_panel_open_when_mixed_ack_state(mocked):
    """AC #2: mixed ack state -> panel is open by default."""
    _, mock_realised_pnl = mocked
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=0.0,
        round_trip_count=0,
        unmatched_sells=[
            _unmatched(1, None),
            _unmatched(2, "2026-08-01T00:00:00+00:00"),
        ],
    )

    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})

    assert resp.status_code == 200
    assert "<details class=" in resp.text
    assert 'class="unmatched-panel mt-3" open' in resp.text


def test_unmatched_panel_collapsed_when_all_acknowledged(mocked):
    """AC #2: every entry acknowledged -> panel is collapsed by default (no
    ``open`` attribute)."""
    _, mock_realised_pnl = mocked
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=0.0,
        round_trip_count=0,
        unmatched_sells=[_unmatched(1, "2026-08-01T00:00:00+00:00")],
    )

    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})

    assert resp.status_code == 200
    assert "<details class=" in resp.text
    assert 'class="unmatched-panel mt-3" open' not in resp.text


def test_ack_toggle_button_copy_unacknowledged_vs_acknowledged(mocked):
    """AC #4/#6: exact button copy for each state."""
    _, mock_realised_pnl = mocked
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=0.0,
        round_trip_count=0,
        unmatched_sells=[_unmatched(1, None)],
    )

    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})

    assert "Mark as known transfer" in resp.text
    assert "Dismiss" not in resp.text
    assert "Ignore" not in resp.text

    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={},
        total_realised_pnl_gbp=0.0,
        round_trip_count=0,
        unmatched_sells=[_unmatched(1, "2026-08-09T12:00:00+00:00")],
    )

    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})

    assert "Undo" in resp.text
    assert "Acknowledged 2026-08-09" in resp.text
