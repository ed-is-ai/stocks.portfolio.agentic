"""Regression tests for the canonical watchlist UI and refresh flow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.app import app
from app.api.dependencies import (
    get_pipeline_service,
    get_portfolio_service,
    get_trader_service,
)
from app.api.routes.pipeline import refresh_data
from app.api.routes.views import partial_watchlist
from app.services.pipeline_service import PipelineRunResult


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "api" / "templates"


def _request(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
        }
    )


def test_legacy_scanner_ui_artifacts_are_removed() -> None:
    assert not (TEMPLATES / "_scanner.html").exists()

    forbidden = ("_scanner.html", "/partials/scanner", "python orchestrator.py")
    source_paths = [
        *sorted((ROOT / "app" / "api").rglob("*.py")),
        *sorted(TEMPLATES.rglob("*.html")),
        ROOT / "run.md",
    ]
    for path in source_paths:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"legacy marker {marker!r} remains in {path}"


def test_dashboard_has_one_refresh_data_control() -> None:
    markup = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TEMPLATES.rglob("*.html"))
    )
    assert markup.count('id="refresh-data-button"') == 1


def test_removed_scanner_partial_returns_not_found() -> None:
    response = TestClient(app).get("/partials/scanner")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_initial_and_completed_refresh_use_equivalent_watchlist_context() -> None:
    records = []
    trader = MagicMock()
    trader.get_portfolio.return_value = [SimpleNamespace(ticker="AAPL")]
    portfolio = MagicMock()
    portfolio.load_analysis.return_value = records
    pipeline = MagicMock()
    pipeline.missing_configuration.return_value = []
    pipeline.run_once.return_value = PipelineRunResult(success=True, details="done")

    initial = await partial_watchlist(
        _request("/partials/watchlist"), trader, portfolio
    )
    refreshed = await refresh_data(
        _request("/refresh-data", method="POST"),
        trader,
        portfolio,
        pipeline,
        confirm_missing=False,
    )

    assert initial.template.name == refreshed.template.name == "_watchlist.html"
    # Records are sorted actionable-first, so both paths return an equivalent
    # (equal) list rather than the same object as load_analysis returned.
    assert initial.context["records"] == refreshed.context["records"] == records
    assert (
        initial.context["portfolio_tickers"]
        == refreshed.context["portfolio_tickers"]
        == {"AAPL"}
    )


def test_watchlist_frontend_assets_are_served() -> None:
    client = TestClient(app)
    for path in (
        "/static/css/watchlist.css",
        "/static/js/watchlist.js",
        "/static/js/pipeline-refresh.js",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.text.strip(), path


def test_watchlist_and_refresh_http_paths_render_canonical_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trader = MagicMock()
    trader.get_portfolio.return_value = []
    portfolio = MagicMock()
    portfolio.load_analysis.return_value = []
    pipeline = MagicMock()
    pipeline.missing_configuration.return_value = []
    pipeline.run_once.return_value = PipelineRunResult(success=True, details="done")
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: portfolio
    app.dependency_overrides[get_pipeline_service] = lambda: pipeline
    monkeypatch.setenv("APP_AUTH_TOKEN", "test-token")

    try:
        client = TestClient(app)
        initial = client.get("/partials/watchlist")
        refreshed = client.post(
            "/refresh-data",
            data={"confirm_missing": "true"},
            headers={"X-Auth-Token": "test-token"},
        )
    finally:
        app.dependency_overrides.clear()

    assert initial.status_code == refreshed.status_code == 200
    for response in (initial, refreshed):
        assert "No analysis results found" in response.text
        assert 'id="refresh-data-button"' not in response.text
