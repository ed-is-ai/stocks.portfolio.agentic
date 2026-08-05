"""Tests for the POST /portfolio/quick-add route (#148).

Adds a holding from a ticker + GBP value, computing shares from the live price
and recording a BUY at the native price. Prices and the trader are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_portfolio_service, get_trader_service

client = TestClient(app)


@pytest.fixture
def mocked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: HTMLResponse("ok", status_code=k.get("status_code", 200)),
    )
    mock_trader = MagicMock()
    mock_trader.portfolio_exists.return_value = True
    mock_portfolio = MagicMock()
    mock_portfolio.gbpusd_rate.return_value = 1.25
    mock_portfolio.load_ticker_aliases.return_value = {}
    mock_portfolio.default_portfolio_context.return_value = {}
    # AAPL: £2.00/share GBP, native $2.50 USD
    mock_portfolio.fetch_all_prices.return_value = (
        {"AAPL": 2.0},
        {"AAPL": (2.5, "USD")},
    )
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    try:
        yield mock_trader, mock_portfolio
    finally:
        app.dependency_overrides.clear()


def _post(ticker: str = "aapl", value: str = "100", token: str = "s3cret"):
    headers = {"X-Auth-Token": token} if token else {}
    return client.post(
        "/portfolio/quick-add",
        data={"ticker": ticker, "value": value, "portfolio_id": "1"},
        headers=headers,
    )


def test_happy_path_computes_shares_and_buys_at_native_price(mocked):
    mock_trader, _ = mocked
    resp = _post(ticker="aapl", value="100")

    assert resp.status_code == 200
    # shares = value(£100) / gbp_price(£2) = 50; price stored is native USD 2.5
    args = mock_trader.record_buy.call_args
    assert args.args[0] == "AAPL"
    assert args.args[1] == pytest.approx(50.0)
    assert args.args[2] == pytest.approx(2.5)
    # fetched price persisted so the new holding renders
    mock_trader.save_price_cache.assert_called_once()


def test_price_unavailable_records_nothing(mocked):
    mock_trader, mock_portfolio = mocked
    mock_portfolio.fetch_all_prices.return_value = ({}, {})
    resp = _post(ticker="ZZZZ", value="100")

    assert resp.status_code == 400
    mock_trader.record_buy.assert_not_called()


def test_non_positive_value_rejected(mocked):
    mock_trader, mock_portfolio = mocked
    resp = _post(ticker="AAPL", value="0")

    assert resp.status_code == 400
    mock_trader.record_buy.assert_not_called()
    mock_portfolio.fetch_all_prices.assert_not_called()


def test_blank_ticker_rejected(mocked):
    mock_trader, _ = mocked
    resp = _post(ticker="   ", value="100")

    assert resp.status_code == 400
    mock_trader.record_buy.assert_not_called()


def test_forbidden_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post("/portfolio/quick-add", data={"ticker": "AAPL", "value": "100"})
    assert resp.status_code == 403
