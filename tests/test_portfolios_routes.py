"""Tests for POST /portfolios/{id}/delete (#186).

Regression guard: deleting a portfolio removes its trades/cash flows/cash
balance (via TraderAgent) but must NOT delete its prior notification-centre
events (they're history, not live account data) — instead it records one
new event documenting the deletion itself, for an audit trail.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_notifications_repository,
    get_portfolio_service,
    get_trader_service,
)
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.trade import Portfolio

client = TestClient(app)
_AUTH = {"X-Auth-Token": "s3cret"}


@pytest.fixture
def mocked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.portfolios.templates.TemplateResponse",
        lambda *a, **k: HTMLResponse("ok", status_code=k.get("status_code", 200)),
    )
    mock_trader = MagicMock()
    mock_trader.get_portfolio_meta.return_value = Portfolio(
        id=7,
        name="SIPP",
        created_at="2024-01-01",
        trade_count=12,
        cash_flow_count=5,
    )
    mock_trader.get_cash_balance.return_value = 1234.5
    mock_portfolio = MagicMock()
    mock_portfolio.default_portfolio_context.return_value = {}
    mock_notifications = MagicMock()
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    app.dependency_overrides[get_notifications_repository] = lambda: mock_notifications
    try:
        yield mock_trader, mock_portfolio, mock_notifications
    finally:
        app.dependency_overrides.clear()


def test_delete_portfolio_removes_its_data_and_keeps_notifications(mocked):
    mock_trader, _, mock_notifications = mocked

    resp = client.post("/portfolios/7/delete", headers=_AUTH)

    assert resp.status_code == 200
    mock_trader.delete_portfolio.assert_called_once_with(7)
    # The only notifications-repo interaction is recording the audit event
    # below — prior events for this account are never deleted.
    assert len(mock_notifications.method_calls) == 1
    assert mock_notifications.method_calls[0][0] == "record"


def test_delete_portfolio_records_audit_notification(mocked):
    _, _, mock_notifications = mocked

    resp = client.post("/portfolios/7/delete", headers=_AUTH)

    assert resp.status_code == 200
    mock_notifications.record.assert_called_once()
    args, kwargs = mock_notifications.record.call_args
    assert args[0] == NotificationCategory.PORTFOLIO
    assert args[1] == "portfolio_deleted"
    assert "SIPP" in args[2]
    assert kwargs["severity"] == NotificationSeverity.WARNING
    assert "12 trade(s)" in kwargs["body"]
    assert "5 cash flow(s)" in kwargs["body"]
    assert "1,234.50" in kwargs["body"]
    assert kwargs["portfolio_id"] == 7


def test_delete_portfolio_requires_auth(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post("/portfolios/7/delete", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


# --- #541: the refresh response honours the browser's chart range ----------


@pytest.fixture
def refresh_mocks(monkeypatch: pytest.MonkeyPatch):
    """A refresh with one priced position, rendered through a stub template."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: HTMLResponse("ok", status_code=k.get("status_code", 200)),
    )
    snapshot = MagicMock(cash_balance=1000.0)
    chart_snapshot = MagicMock(cash_balance=1000.0)
    positions = [MagicMock(ticker="AAPL")]
    mock_trader = MagicMock()
    mock_trader.load_price_cache.return_value = ({"AAPL": 100.0}, "now", {})
    mock_portfolio = MagicMock()
    mock_portfolio.portfolio_input_snapshot.return_value = snapshot
    mock_portfolio.with_current_chart_data.return_value = chart_snapshot
    mock_portfolio.positions_from_input_snapshot.return_value = positions
    mock_portfolio.gbpusd_rate.return_value = 1.25
    mock_portfolio.load_ticker_aliases.return_value = {}
    mock_portfolio.fetch_all_prices_with_failures.return_value = (
        {"AAPL": 100.0},
        {},
        set(),
    )
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    try:
        yield mock_trader, mock_portfolio, snapshot, chart_snapshot
    finally:
        app.dependency_overrides.clear()


def test_refresh_renders_the_chart_on_the_supplied_range(refresh_mocks):
    """The re-rendered partial stays on the range the user is looking at."""
    _, mock_portfolio, snapshot, chart_snapshot = refresh_mocks

    resp = client.post("/api/portfolio/refresh?portfolio_id=7&range=3M", headers=_AUTH)

    assert resp.status_code == 200
    mock_portfolio.portfolio_input_snapshot.assert_called_once_with(7, range_key="3M")
    mock_portfolio.with_current_chart_data.assert_called_once_with(snapshot, 7, "3M")
    context_kwargs = mock_portfolio.portfolio_partial_context.call_args.kwargs
    assert context_kwargs["range_key"] == "3M"
    assert context_kwargs["input_snapshot"] is chart_snapshot


def test_refresh_chart_data_is_reread_after_the_new_snapshot_is_written(refresh_mocks):
    """The response's chart must include the point this refresh just wrote.

    ``with_current_chart_data`` is that re-read, so it has to happen *after*
    ``update_portfolio_snapshot`` -- otherwise the swapped-in card shows the
    pre-refresh series and only a browser reload would reveal the new point.
    """
    mock_trader, mock_portfolio, _, _ = refresh_mocks
    order = MagicMock()
    order.attach_mock(mock_trader.update_portfolio_snapshot, "write")
    order.attach_mock(mock_portfolio.with_current_chart_data, "reread")

    resp = client.post("/api/portfolio/refresh?portfolio_id=7&range=3M", headers=_AUTH)

    assert resp.status_code == 200
    assert [call[0] for call in order.mock_calls] == ["write", "reread"]


def test_refresh_falls_back_to_the_default_range_for_an_unknown_token(refresh_mocks):
    _, mock_portfolio, snapshot, _ = refresh_mocks

    resp = client.post("/api/portfolio/refresh?range=nonsense", headers=_AUTH)

    assert resp.status_code == 200
    mock_portfolio.with_current_chart_data.assert_called_once_with(
        snapshot, None, "12M"
    )
