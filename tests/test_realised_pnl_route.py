"""Tests for GET /partials/realised-pnl (#177).

Regression-guards AC2 against the #147/#169 empty-string ``portfolio_id``
422 bug class, and smoke-tests the no-portfolios / zero-round-trips states.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_realised_pnl_service, get_trader_service
from app.schemas import Portfolio, RealisedPnlSummary, RoundTrip, UnmatchedSell

client = TestClient(app)
_AUTH = {"X-Auth-Token": "s3cret"}


def _stat_card(html: str, label: str) -> str:
    match = re.search(
        rf'<div class="stat-card">\s*<div class="slbl">.*?{re.escape(label)}</div>'
        r'\s*<div class="sval">(.*?)</div>\s*</div>',
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
    assert "0 Wins" in resp.text
    assert "0 Losses" in resp.text
    assert "Avg Win % / Avg Loss %" in resp.text
    average_card = _stat_card(resp.text, "Avg Win % / Avg Loss %")
    assert average_card.count("&mdash;") == 2
    assert 'class="pos"' not in average_card
    assert 'class="neg"' not in average_card


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


def test_round_trip_details_are_collapsed_and_retain_all_row_content(mocked):
    _, mock_realised_pnl = mocked
    mock_realised_pnl.compute_summary.return_value = RealisedPnlSummary(
        portfolio_id=1,
        round_trips={
            "WIN": [
                _round_trip("WIN", 10.0, exit_date="2026-02-02"),
                _round_trip("WIN", 0.0, exit_date="2026-02-01"),
            ],
            "LOSS": [_round_trip("LOSS", -5.0)],
            "USDX": [_round_trip("USDX", 0.0, fx_unavailable=True)],
        },
        total_realised_pnl_gbp=5.0,
        round_trip_count=4,
        winning_round_trip_count=2,
        losing_round_trip_count=1,
        average_win_pct=5.0,
        average_loss_pct=-5.0,
    )

    resp = client.get("/partials/realised-pnl", params={"portfolio_id": "1"})

    assert resp.status_code == 200
    assert "2 Wins" in resp.text
    assert "1 Loss" in resp.text
    assert "+5.0%" in resp.text
    assert "-5.0%" in resp.text
    average_card = _stat_card(resp.text, "Avg Win % / Avg Loss %")
    assert 'class="pos">+5.0%</span>' in average_card
    assert 'class="neg">-5.0%</span>' in average_card
    assert resp.text.index("Win / Loss") < resp.text.index("Avg Win % / Avg Loss %")
    assert resp.text.index("Avg Win % / Avg Loss %") < resp.text.index(
        "Unmatched Sells"
    )
    assert "WIN subtotal" in resp.text
    assert "LOSS subtotal" in resp.text
    assert "USDX subtotal" in resp.text
    details_tags = re.findall(r"<details\b[^>]*\bticker-detail\b[^>]*>", resp.text)
    assert len(details_tags) == 3
    assert all("open" not in tag.split() for tag in details_tags)
    assert "2 round-trips" in resp.text
    assert "FX rate unavailable" in resp.text
    assert "+£5.00" in resp.text
    assert "2026-01-01" in resp.text
    assert "100.00" in resp.text
    assert resp.text.index("2026-02-02") < resp.text.index("2026-02-01")
    assert "110.00" in resp.text
    assert "+£10.00" in resp.text
    assert "+0.0%" in resp.text
    assert resp.text.index("WIN subtotal") < resp.text.index("LOSS subtotal")
    assert resp.text.index("LOSS subtotal") < resp.text.index("USDX subtotal")


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
