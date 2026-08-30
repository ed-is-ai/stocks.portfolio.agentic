"""Tests for request-parameter coercion and the empty-portfolio_id regression.

The web UI sends an empty ``portfolio_id=`` when no account is selected; an
``int | None`` route param rejected that with 422 and broke the Portfolio tab.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_portfolio_service
from app.api.params import optional_int

client = TestClient(app)


def test_optional_int_coerces():
    assert optional_int(None) is None
    assert optional_int("") is None
    assert optional_int("   ") is None
    assert optional_int("abc") is None  # non-numeric -> None, not an error
    assert optional_int("5") == 5
    assert optional_int(" 42 ") == 42


@pytest.fixture
def mocked_portfolio(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.api.routes.views.templates.TemplateResponse",
        lambda *a, **k: HTMLResponse("ok"),
    )
    mock = MagicMock()
    mock.default_portfolio_context.return_value = {}
    app.dependency_overrides[get_portfolio_service] = lambda: mock
    try:
        yield mock
    finally:
        app.dependency_overrides.clear()


def test_partial_portfolio_accepts_empty_portfolio_id(mocked_portfolio):
    # The regression: an empty ?portfolio_id= must render (200), not 422.
    resp = client.get("/partials/portfolio?portfolio_id=")
    assert resp.status_code == 200
    mocked_portfolio.default_portfolio_context.assert_called_once_with(None, range_key="12M")


def test_partial_portfolio_accepts_numeric_portfolio_id(mocked_portfolio):
    resp = client.get("/partials/portfolio?portfolio_id=3")
    assert resp.status_code == 200
    mocked_portfolio.default_portfolio_context.assert_called_once_with(3, range_key="12M")


def test_partial_portfolio_accepts_no_portfolio_id(mocked_portfolio):
    resp = client.get("/partials/portfolio")
    assert resp.status_code == 200
    mocked_portfolio.default_portfolio_context.assert_called_once_with(None, range_key="12M")
