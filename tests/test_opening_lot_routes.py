"""Tests for the Opening Lot routes (create/edit/delete) and the Match
Trace detail view (Story 2.4).

Follows ``tests/test_portfolio_quick_add.py``'s pattern: a real
``TestClient`` against the app, with ``TraderService``/``PortfolioService``/
``RealisedPnlService`` mocked via dependency overrides so each route's own
logic (validation, gating, response shape) is exercised without touching a
real database.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.agents.trader.trader_agent import OpeningLotDuplicateError
from app.api.app import app
from app.api.dependencies import (
    get_portfolio_service,
    get_realised_pnl_service,
    get_trader_service,
)
from app.schemas.trade import Trade

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
    mock_trader.record_opening_lot.return_value = Trade(
        id=1,
        ticker="AAPL",
        action="BUY",
        shares=10,
        price=100.0,
        date="2026-01-01",
        portfolio_id=1,
        source="opening_lot",
    )
    mock_trader.update_opening_lot.return_value = Trade(
        id=5,
        ticker="AAPL",
        action="BUY",
        shares=15,
        price=110.0,
        date="2026-01-02",
        portfolio_id=1,
        source="opening_lot",
    )
    mock_portfolio = MagicMock()
    mock_portfolio.default_portfolio_context.return_value = {}
    mock_realised_pnl = MagicMock()
    mock_realised_pnl.opening_lot_status.return_value = "unconsumed"
    # No same-day-unmatched-sell scenario by default -- individual tests
    # override this to exercise ``_same_day_ordering_warning``.
    mock_realised_pnl.compute_summary.return_value = SimpleNamespace(unmatched_sells=[])
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    app.dependency_overrides[get_realised_pnl_service] = lambda: mock_realised_pnl
    try:
        yield mock_trader, mock_portfolio, mock_realised_pnl
    finally:
        app.dependency_overrides.clear()


_HEADERS = {"X-Auth-Token": "s3cret"}


# --- create --------------------------------------------------------------


def test_create_opening_lot_happy_path(mocked):
    mock_trader, _, _ = mocked
    resp = client.post(
        "/portfolio/opening-lot",
        data={
            "ticker": "aapl",
            "shares": "10",
            "price": "100",
            "date": "2026-01-01",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    mock_trader.record_opening_lot.assert_called_once_with(
        "AAPL", 10.0, 100.0, "2026-01-01", "", 1
    )


def test_create_opening_lot_missing_portfolio_rejected(mocked):
    mock_trader, _, _ = mocked
    mock_trader.portfolio_exists.return_value = False
    resp = client.post(
        "/portfolio/opening-lot",
        data={
            "ticker": "AAPL",
            "shares": "10",
            "price": "100",
            "date": "2026-01-01",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 400
    mock_trader.record_opening_lot.assert_not_called()


def test_create_opening_lot_non_positive_shares_rejected(mocked):
    mock_trader, _, _ = mocked
    resp = client.post(
        "/portfolio/opening-lot",
        data={
            "ticker": "AAPL",
            "shares": "0",
            "price": "100",
            "date": "2026-01-01",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 400
    mock_trader.record_opening_lot.assert_not_called()


def test_create_opening_lot_duplicate_rejected_with_error_banner(mocked):
    """AC4: a duplicate Opening Lot is rejected with a clear validation
    error, not silently inserted."""
    mock_trader, mock_portfolio, _ = mocked
    mock_trader.record_opening_lot.side_effect = OpeningLotDuplicateError(
        "An Opening Lot for AAPL on 2026-01-01 with 10 shares already exists."
    )

    resp = client.post(
        "/portfolio/opening-lot",
        data={
            "ticker": "AAPL",
            "shares": "10",
            "price": "100",
            "date": "2026-01-01",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 400
    context = mock_portfolio.default_portfolio_context.call_args
    assert context is not None


def test_create_opening_lot_warns_on_same_day_ordering_gap(mocked):
    """Documented gap (deferred-work.md): a same-date Opening Lot can't
    retroactively resolve a same-date unmatched sell, because the
    same-day replay tie-break always places the (earlier-written) sell
    before the (later-written) lot. When the newly-created lot is still
    entirely untouched and an unmatched sell remains for the exact same
    ticker/date, the route warns instead of reporting a plain success."""
    mock_trader, mock_portfolio, mock_realised_pnl = mocked
    mock_trader.record_opening_lot.return_value = Trade(
        id=42,
        ticker="AAPL",
        action="BUY",
        shares=10,
        price=100.0,
        date="2026-02-01",
        portfolio_id=1,
        source="opening_lot",
    )
    mock_realised_pnl.opening_lot_status.return_value = "unconsumed"
    mock_realised_pnl.compute_summary.return_value = SimpleNamespace(
        unmatched_sells=[SimpleNamespace(ticker="AAPL", date="2026-02-01")]
    )

    resp = client.post(
        "/portfolio/opening-lot",
        data={
            "ticker": "AAPL",
            "shares": "10",
            "price": "100",
            "date": "2026-02-01",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    context = mock_portfolio.default_portfolio_context.return_value
    assert context["import_status"] == "warning"
    assert "same-day" in context["import_message"]


def test_create_opening_lot_no_warning_when_lot_was_consumed(mocked):
    """If the new lot actually got matched (even partially), the
    same-day ordering gap didn't occur -- any remaining unmatched sell for
    that date is a genuine shortfall, not this bug, so no warning."""
    mock_trader, mock_portfolio, mock_realised_pnl = mocked
    mock_trader.record_opening_lot.return_value = Trade(
        id=42,
        ticker="AAPL",
        action="BUY",
        shares=10,
        price=100.0,
        date="2026-02-01",
        portfolio_id=1,
        source="opening_lot",
    )
    mock_realised_pnl.opening_lot_status.return_value = "consumed"
    mock_realised_pnl.compute_summary.return_value = SimpleNamespace(
        unmatched_sells=[SimpleNamespace(ticker="AAPL", date="2026-02-01")]
    )

    resp = client.post(
        "/portfolio/opening-lot",
        data={
            "ticker": "AAPL",
            "shares": "10",
            "price": "100",
            "date": "2026-02-01",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    context = mock_portfolio.default_portfolio_context.return_value
    assert "import_status" not in context


def test_create_opening_lot_forbidden_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post(
        "/portfolio/opening-lot",
        data={
            "ticker": "AAPL",
            "shares": "10",
            "price": "100",
            "date": "2026-01-01",
            "portfolio_id": "1",
        },
    )
    assert resp.status_code == 403


# --- edit ------------------------------------------------------------------


def test_edit_opening_lot_happy_path_when_unconsumed(mocked):
    mock_trader, mock_portfolio, mock_realised_pnl = mocked
    mock_realised_pnl.opening_lot_status.return_value = "unconsumed"

    resp = client.post(
        "/portfolio/opening-lot/5/edit",
        data={
            "ticker": "AAPL",
            "shares": "15",
            "price": "110",
            "date": "2026-01-02",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    # Called twice: once for the pre-write consumed/unconsumed gate, once
    # more inside ``_same_day_ordering_warning``'s post-write check.
    mock_realised_pnl.opening_lot_status.assert_any_call(5, 1)
    mock_trader.update_opening_lot.assert_called_once_with(
        5, "AAPL", 15.0, 110.0, "2026-01-02", "", 1
    )
    assert "import_status" not in mock_portfolio.default_portfolio_context.return_value


def test_edit_opening_lot_non_positive_shares_rejected(mocked):
    """Mirrors ``create_opening_lot``'s validation guard -- a submitted
    non-positive shares/price is rejected before the consumed/unconsumed
    check or any write, not silently written through."""
    mock_trader, _, mock_realised_pnl = mocked

    resp = client.post(
        "/portfolio/opening-lot/5/edit",
        data={
            "ticker": "AAPL",
            "shares": "0",
            "price": "110",
            "date": "2026-01-02",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 400
    mock_realised_pnl.opening_lot_status.assert_not_called()
    mock_trader.update_opening_lot.assert_not_called()


def test_edit_opening_lot_non_positive_price_rejected(mocked):
    mock_trader, _, _ = mocked

    resp = client.post(
        "/portfolio/opening-lot/5/edit",
        data={
            "ticker": "AAPL",
            "shares": "15",
            "price": "0",
            "date": "2026-01-02",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 400
    mock_trader.update_opening_lot.assert_not_called()


def test_edit_opening_lot_warns_on_same_day_ordering_gap(mocked):
    """Same gap as ``create_opening_lot``'s: editing a lot's date to land
    on the same day as an unmatched sell for the same ticker can silently
    fail to resolve it. The route warns instead of a plain success."""
    mock_trader, mock_portfolio, mock_realised_pnl = mocked
    mock_trader.update_opening_lot.return_value = Trade(
        id=5,
        ticker="AAPL",
        action="BUY",
        shares=15,
        price=110.0,
        date="2026-02-01",
        portfolio_id=1,
        source="opening_lot",
    )
    mock_realised_pnl.opening_lot_status.return_value = "unconsumed"
    mock_realised_pnl.compute_summary.return_value = SimpleNamespace(
        unmatched_sells=[SimpleNamespace(ticker="AAPL", date="2026-02-01")]
    )

    resp = client.post(
        "/portfolio/opening-lot/5/edit",
        data={
            "ticker": "AAPL",
            "shares": "15",
            "price": "110",
            "date": "2026-02-01",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    context = mock_portfolio.default_portfolio_context.return_value
    assert context["import_status"] == "warning"
    assert "same-day" in context["import_message"]


def test_edit_opening_lot_rejected_when_consumed(mocked):
    """AC7/AC8: any part consumed by a match rejects the edit with a clear
    error and no mutation -- never a silent no-op."""
    mock_trader, _, mock_realised_pnl = mocked
    mock_realised_pnl.opening_lot_status.return_value = "consumed"

    resp = client.post(
        "/portfolio/opening-lot/5/edit",
        data={
            "ticker": "AAPL",
            "shares": "15",
            "price": "110",
            "date": "2026-01-02",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 400
    mock_trader.update_opening_lot.assert_not_called()


def test_edit_opening_lot_rejected_when_status_none(mocked):
    """A stale/unknown trade id (``opening_lot_status`` returns ``None``)
    is treated the same as consumed -- rejected, not mutated."""
    mock_trader, _, mock_realised_pnl = mocked
    mock_realised_pnl.opening_lot_status.return_value = None

    resp = client.post(
        "/portfolio/opening-lot/999/edit",
        data={
            "ticker": "AAPL",
            "shares": "15",
            "price": "110",
            "date": "2026-01-02",
            "portfolio_id": "1",
        },
        headers=_HEADERS,
    )

    assert resp.status_code == 400
    mock_trader.update_opening_lot.assert_not_called()


# --- delete ------------------------------------------------------------------


def test_delete_opening_lot_happy_path_when_unconsumed(mocked):
    mock_trader, _, mock_realised_pnl = mocked
    mock_realised_pnl.opening_lot_status.return_value = "unconsumed"

    resp = client.request(
        "DELETE",
        "/portfolio/opening-lot/5?portfolio_id=1",
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    mock_trader.delete_opening_lot.assert_called_once_with(5, 1)


def test_delete_opening_lot_rejected_when_consumed(mocked):
    mock_trader, _, mock_realised_pnl = mocked
    mock_realised_pnl.opening_lot_status.return_value = "consumed"

    resp = client.request(
        "DELETE",
        "/portfolio/opening-lot/5?portfolio_id=1",
        headers=_HEADERS,
    )

    assert resp.status_code == 400
    mock_trader.delete_opening_lot.assert_not_called()


def test_delete_opening_lot_forbidden_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.request("DELETE", "/portfolio/opening-lot/5?portfolio_id=1")
    assert resp.status_code == 403


# --- match trace detail view -------------------------------------------------


def test_match_trace_view_renders_trace(mocked):
    _, _, mock_realised_pnl = mocked
    fake_trace = MagicMock()
    mock_realised_pnl.get_match_trace.return_value = fake_trace

    resp = client.get("/portfolio/1/match-trace/42")

    assert resp.status_code == 200
    mock_realised_pnl.get_match_trace.assert_called_once_with(42, 1)


def test_match_trace_view_no_auth_required(mocked):
    """Read-only view, matching the reconciliation view's shape -- no
    ``require_local_or_token`` dependency."""
    _, _, mock_realised_pnl = mocked
    mock_realised_pnl.get_match_trace.return_value = None

    resp = client.get("/portfolio/1/match-trace/42")

    assert resp.status_code == 200


def test_match_trace_view_missing_trace_returns_none_context(mocked):
    _, _, mock_realised_pnl = mocked
    mock_realised_pnl.get_match_trace.return_value = None

    resp = client.get("/portfolio/1/match-trace/42")

    assert resp.status_code == 200
