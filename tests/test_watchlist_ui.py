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
    get_alerts_repository,
    get_pipeline_service,
    get_portfolio_service,
    get_trader_service,
)
from app.api.routes.pipeline import refresh_data
from app.api.routes.views import partial_watchlist
from app.api.templating import templates
from app.schemas.record import StockRecord
from app.schemas.scan import CANSLIMScore, MomentumScore, StockAnalysis
from app.services.pipeline_service import PipelineRunResult


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "api" / "templates"


def _sample_records() -> list[StockRecord]:
    analysis = StockAnalysis(
        score=8,
        stage="Stage 2",
        entry_zone="broken_out",
        entry_price=100.0,
        stop_loss=92.0,
        risk_pct=0.08,
        r_multiples={"1.0R": 108.0, "2.0R": 116.0},
        reward_risk_ratio=2.0,
        volume_confirmed=True,
        fresh_breakout=True,
        sepa_template={
            "above_150_200": True,
            "sma150_above_200": True,
            "sma200_rising": True,
            "sma50_above_150_200": True,
            "above_25pct_of_low": True,
            "within_25pct_of_high": True,
            "rs_leader": True,
            "above_sma50": True,
        },
        canslim=CANSLIMScore(C=2, A=2, N=2, S=2, L=2, I=1, M=1),
        momentum=MomentumScore(C=2, A=2, N=2, S=1, L=2, I=1, M=1),
        summary="test",
    )
    with_analysis = StockRecord(
        ticker="AAPL.L",
        as_of="2026-01-01",
        price=98.0,
        rsi14=62.0,
        volume=1000,
        rel_volume=1.2,
        high_52w=120.0,
        low_52w=60.0,
        pct_from_52w_high=-18.0,
        pct_change_week=2.0,
        sector="Technology",
        analysis=analysis,
    )
    without_analysis = StockRecord(
        ticker="TSLA",
        as_of="2026-01-01",
        price=200.0,
        volume=1000,
        rel_volume=1.0,
        high_52w=300.0,
        low_52w=100.0,
        pct_from_52w_high=-33.0,
        pct_change_week=-1.0,
        analysis=None,
    )
    return [with_analysis, without_analysis]


def _render_watchlist() -> str:
    return templates.get_template("_watchlist.html").render(
        records=_sample_records(), portfolio_tickers={"AAPL.L"}
    )


SORTABLE_COLUMNS = (
    "ticker",
    "rec",
    "score",
    "canslim",
    "momentum",
    "stage",
    "sector",
    "price",
    "entry",
    "vsentry",
    "stop",
    "vsstop",
    "risk",
    "targets",
    "rsi",
    "sepa",
)


def test_watchlist_splits_score_entry_stop_into_sortable_columns() -> None:
    html = _render_watchlist()
    # Each split-out metric now owns a header cell with a stable data-col key.
    for col in (*SORTABLE_COLUMNS, "buy"):
        assert f'data-col="{col}"' in html, f"missing column {col}"
    # Every sortable column has a keyboard-operable sort control.
    for col in SORTABLE_COLUMNS:
        assert f'class="wl-sort" data-col="{col}"' in html, f"no sort button for {col}"


def test_watchlist_cells_carry_canonical_numeric_data_values() -> None:
    html = _render_watchlist()
    # Sorting/filtering read these data-val attributes, never the rendered text.
    assert 'data-col="score" data-val="8"' in html
    assert 'data-col="canslim" data-val="12"' in html
    assert 'data-col="momentum" data-val="11"' in html
    assert 'data-col="sepa" data-val="8"' in html
    assert 'data-col="risk" data-val="8.0000"' in html
    # The Buy-recommendation row exposes its actionability rank for sorting.
    assert 'data-col="rec" data-val="6"' in html


def test_watchlist_missing_values_render_empty_data_val() -> None:
    html = _render_watchlist()
    # The analysis-less TSLA row must leave numeric data-val empty so those
    # cells sort last and are excluded from active range filters.
    assert 'data-col="score" data-val=""' in html
    assert 'data-col="canslim" data-val=""' in html


def test_watchlist_toolbar_exposes_search_and_control_mount() -> None:
    html = _render_watchlist()
    assert 'id="wl-search"' in html
    assert 'id="wl-adv"' in html
    assert 'id="wl-match-count"' in html


def test_watchlist_js_persists_state_and_seeds_presets() -> None:
    js = (ROOT / "app" / "api" / "static" / "js" / "watchlist.js").read_text(
        encoding="utf-8"
    )
    assert "wl-state-v1" in js
    assert "wl-presets-v1" in js
    for preset in ("Buy-ready", "UK breakouts", "Low-risk Stage 2"):
        assert preset in js, f"missing built-in preset {preset}"


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
    alerts = MagicMock()
    alerts.has_watching.return_value = False
    alerts.last_alerted_at.return_value = None

    initial = await partial_watchlist(
        _request("/partials/watchlist"), trader, portfolio, alerts
    )
    refreshed = await refresh_data(
        _request("/refresh-data", method="POST"),
        trader,
        portfolio,
        pipeline,
        alerts,
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
    alerts = MagicMock()
    alerts.has_watching.return_value = False
    alerts.last_alerted_at.return_value = None
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: portfolio
    app.dependency_overrides[get_pipeline_service] = lambda: pipeline
    app.dependency_overrides[get_alerts_repository] = lambda: alerts
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
