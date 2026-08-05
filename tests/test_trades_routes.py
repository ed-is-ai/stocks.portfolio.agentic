"""Characterization tests for the POST /trades action-dispatch route."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_portfolio_service, get_trader_service

client = TestClient(app)

_FORM = {
    "ticker": "AAPL",
    "shares": "10",
    "price": "100",
    "date": "2024-01-01",
    "portfolio_id": "1",
}


@pytest.fixture
def mocked_trades(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.trades.templates.TemplateResponse",
        lambda *a, **k: HTMLResponse("ok", status_code=k.get("status_code", 200)),
    )
    mock_trader = MagicMock()
    mock_trader.portfolio_exists.return_value = True
    mock_portfolio = MagicMock()
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    try:
        yield mock_trader, mock_portfolio
    finally:
        app.dependency_overrides.clear()


def _post(action: str, **extra):
    return client.post(
        "/trades",
        data={**_FORM, "action": action, **extra},
        headers={"X-Auth-Token": "s3cret"},
    )


def test_buy_dispatches_record_buy(mocked_trades):
    mock_trader, _ = mocked_trades
    resp = _post("BUY")
    assert resp.status_code == 200
    mock_trader.record_buy.assert_called_once_with(
        "AAPL", 10.0, 100.0, "2024-01-01", "", None, None, 1
    )
    mock_trader.record_sell.assert_not_called()
    mock_trader.correct_trade.assert_not_called()


def test_sell_dispatches_record_sell(mocked_trades):
    mock_trader, _ = mocked_trades
    resp = _post("SELL")
    assert resp.status_code == 200
    mock_trader.record_sell.assert_called_once_with(
        "AAPL", 10.0, 100.0, "2024-01-01", "", 1
    )
    mock_trader.record_buy.assert_not_called()
    mock_trader.correct_trade.assert_not_called()


def test_correct_dispatches_correct_trade(mocked_trades):
    mock_trader, _ = mocked_trades
    resp = _post("CORRECT")
    assert resp.status_code == 200
    mock_trader.correct_trade.assert_called_once_with(
        "AAPL", 10.0, 100.0, "2024-01-01", "", None, None, 1
    )
    mock_trader.record_buy.assert_not_called()
    mock_trader.record_sell.assert_not_called()


def test_buy_optional_fields_forwarded(mocked_trades):
    mock_trader, _ = mocked_trades
    resp = _post("BUY", notes="hi", stop_loss="90", entry_price="95")
    assert resp.status_code == 200
    mock_trader.record_buy.assert_called_once_with(
        "AAPL", 10.0, 100.0, "2024-01-01", "hi", 90.0, 95.0, 1
    )


def test_unknown_action_is_silent_200_noop(mocked_trades):
    # NOTE: The route silently returns HTTP 200 with no write for unrecognized
    # actions (only logs a warning). This test pins the current behavior —
    # it does not endorse it. If a 4xx is added later, update this test.
    mock_trader, _ = mocked_trades
    resp = _post("FROBNICATE")
    assert resp.status_code == 200
    mock_trader.record_buy.assert_not_called()
    mock_trader.record_sell.assert_not_called()
    mock_trader.correct_trade.assert_not_called()


def test_unknown_portfolio_rejected_no_write(mocked_trades):
    # A stale/unknown portfolio id is rejected with 400 and nothing is written.
    mock_trader, _ = mocked_trades
    mock_trader.portfolio_exists.return_value = False
    resp = _post("BUY")
    assert resp.status_code == 400
    mock_trader.record_buy.assert_not_called()


def test_post_trades_forbidden_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post("/trades", data={**_FORM, "action": "BUY"})
    assert resp.status_code == 403
