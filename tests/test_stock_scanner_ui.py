"""Regression tests for the canonical stock scanner UI and refresh flow."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
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
from app.api.stock_scanner_context import build_stock_scanner_context
from app.api.routes.pipeline import refresh_data
from app.api.routes.views import partial_runlog, partial_stock_scanner
from app.api.templating import templates
from app.repositories.alerts_repo import AlertReadState
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.pipeline_status import PipelineState
from app.schemas.record import StockRecord
from app.schemas.scan import CANSLIMScore, MomentumScore, StockAnalysis
from app.schemas.source_health import SourceHealth, SourceName, SourceState
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
        congress_buys=3,
        congress_sells=13,
        senate_buys=1,
        senate_sells=4,
        funds_buying=12,
        funds_selling=4,
        funds_net=8,
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


def _render_stock_scanner(*, market_narrative=None, market_regime=None) -> str:
    from app.core.alerting import build_alert_ui_state
    from app.core.recommendation import classify_recommendation
    from app.services.freshness_service import calculate_freshness

    records = _sample_records()
    portfolio_tickers = {"AAPL.L"}
    recommendations = {
        record.ticker: classify_recommendation(
            record, is_portfolio_holding=record.ticker in portfolio_tickers
        )
        for record in records
    }
    alert_states = {
        record.ticker: build_alert_ui_state(
            record, has_watching=False, last_alerted_at=None
        )
        for record in records
    }
    return templates.get_template("_stock_scanner.html").render(
        records=records,
        portfolio_tickers=portfolio_tickers,
        recommendations=recommendations,
        alert_states=alert_states,
        freshness=calculate_freshness(None),
        source_health=[],
        latest_attempt_error=None,
        market_narrative=market_narrative,
        market_regime=market_regime,
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
    "congress",
    "funds",
)


def test_stock_scanner_splits_score_entry_stop_into_sortable_columns() -> None:
    html = _render_stock_scanner()
    # Each split-out metric now owns a header cell with a stable data-col key.
    for col in (*SORTABLE_COLUMNS, "buy"):
        assert f'data-col="{col}"' in html, f"missing column {col}"
    # Every sortable column has a keyboard-operable sort control.
    for col in SORTABLE_COLUMNS:
        assert f'class="wl-sort" data-col="{col}"' in html, f"no sort button for {col}"


def test_stock_scanner_cells_carry_canonical_numeric_data_values() -> None:
    html = _render_stock_scanner()
    # Sorting/filtering read these data-val attributes, never the rendered text.
    assert 'data-col="score" data-val="8"' in html
    assert 'data-col="canslim" data-val="12"' in html
    assert 'data-col="momentum" data-val="11"' in html
    assert 'data-col="sepa" data-val="8"' in html
    assert 'data-col="risk" data-val="8.0000"' in html
    # The Buy-recommendation row exposes its actionability rank for sorting
    # (mirrors app.core.recommendation._BUCKET_RANK, the canonical source).
    assert 'data-col="rec" data-val="5"' in html


def test_scanner_context_batches_alert_reads_for_multi_ticker_records() -> None:
    records = [*_sample_records(), _sample_records()[0]]
    trader = MagicMock()
    trader.held_tickers.return_value = set()
    portfolio = MagicMock()
    portfolio.load_analysis.return_value = records
    alerts = MagicMock()
    alerts.states_for_tickers.return_value = {
        "AAPL.L": AlertReadState(has_watching=True, last_alerted_at=None),
        "TSLA": AlertReadState(has_watching=False, last_alerted_at=None),
    }

    context = build_stock_scanner_context(trader, portfolio, alerts)

    assert context["alert_states"]["AAPL.L"].suppressed is True
    # The regime banner reads this key; a rename/typo would slip past the
    # template-only tests otherwise (#387).
    assert "market_regime" in context
    alerts.states_for_tickers.assert_called_once_with(("AAPL.L", "AAPL.L", "TSLA"))
    alerts.has_watching.assert_not_called()
    alerts.last_alerted_at.assert_not_called()


def test_stock_scanner_prices_use_currency_symbol() -> None:
    """UK (GBP) rows render £, US (USD) rows render $ (#123)."""
    from app.core.alerting import build_alert_ui_state
    from app.core.recommendation import classify_recommendation
    from app.services.freshness_service import calculate_freshness

    def _row(ticker: str, currency: str, price: float) -> StockRecord:
        return StockRecord(
            ticker=ticker,
            as_of="2026-01-01",
            price=price,
            currency=currency,
            volume=1000,
            rel_volume=1.0,
            high_52w=price * 1.2,
            low_52w=price * 0.5,
            pct_from_52w_high=-10.0,
            pct_change_week=1.0,
            analysis=None,
        )

    records = [_row("ULVR.L", "GBP", 45.0), _row("AAPL", "USD", 100.0)]
    html = templates.get_template("_stock_scanner.html").render(
        records=records,
        portfolio_tickers=set(),
        recommendations={
            r.ticker: classify_recommendation(r, is_portfolio_holding=False)
            for r in records
        },
        alert_states={
            r.ticker: build_alert_ui_state(r, has_watching=False, last_alerted_at=None)
            for r in records
        },
        freshness=calculate_freshness(None),
        source_health=[],
        latest_attempt_error=None,
    )

    assert "£45.00" in html
    assert "$100.00" in html


def test_stock_scanner_missing_values_render_empty_data_val() -> None:
    html = _render_stock_scanner()
    # The analysis-less TSLA row must leave numeric data-val empty so those
    # cells sort last and are excluded from active range filters.
    assert 'data-col="score" data-val=""' in html
    assert 'data-col="canslim" data-val=""' in html


def test_stock_scanner_congress_column_renders_net_and_tooltip() -> None:
    html = _render_stock_scanner()
    # Net = combined buys − sells (3 − 13 = -10); Senate counts appear in the tip.
    assert 'data-col="congress" data-val="-10"' in html
    assert "Congress: 3 buys / 13 sells" in html
    assert "Senate: 1 buys / 4 sells" in html


def test_stock_scanner_congress_empty_when_no_data() -> None:
    html = _render_stock_scanner()
    # The analysis-less TSLA row has no congressional data → empty, sorts last.
    assert 'data-col="congress" data-val=""' in html


def test_stock_scanner_funds_column_renders_net_and_tooltip() -> None:
    html = _render_stock_scanner()
    # Net comes straight from the precomputed funds_net (8); the tooltip breaks
    # out adding vs trimming filers.
    assert 'data-col="funds" data-val="8"' in html
    assert "Funds: 12 adding / 4 trimming (last quarter 13F)" in html
    assert "wl-col-funds wl-qualify" in html


def test_stock_scanner_funds_empty_when_no_data() -> None:
    html = _render_stock_scanner()
    # The analysis-less TSLA row has no 13F data → empty, sorts last like Congress.
    assert 'data-col="funds" data-val=""' in html


def test_stock_scanner_funds_column_is_sortable_and_grouped() -> None:
    html = _render_stock_scanner()
    assert 'class="wl-sort" data-col="funds"' in html
    # #394 Qualify/Execute group-label row.
    assert 'class="wl-group-label wl-group-qualify"' in html
    assert 'class="wl-group-label wl-group-execute"' in html


def test_stock_scanner_group_headers_declare_bands_and_js_resyncs_colspan() -> None:
    html = _render_stock_scanner()
    # Each group <th> carries an id and the data-col keys in its band so the
    # controller can recompute colSpan after hiding columns.
    assert 'id="wl-group-qualify"' in html
    assert 'id="wl-group-execute"' in html
    assert (
        'data-cols="ticker,sector,score,stage,sepa,rsi,congress,funds,canslim,momentum"'
        in html
    )
    assert 'data-cols="rec,price,vsentry,entry,stop,targets,risk,vsstop,buy"' in html

    js = (ROOT / "app" / "api" / "static" / "js" / "watchlist.js").read_text(
        encoding="utf-8"
    )
    assert "tr.wl-group-row th[data-cols]" in js
    assert "th.colSpan = cols.filter((c) => !hidden.has(c)).length" in js


def test_stock_scanner_rsi_meter_replaces_bare_number() -> None:
    html = _render_stock_scanner()
    assert "wl-rsi-track" in html
    assert "wl-rsi-marker" in html


def test_stock_scanner_toolbar_exposes_search_and_control_mount() -> None:
    html = _render_stock_scanner()
    assert 'id="wl-search"' in html
    assert 'id="wl-adv"' in html
    assert 'id="wl-match-count"' in html


def test_stock_scanner_columns_carry_semantic_classes_not_nth_child() -> None:
    """Every header and data cell gets a stable wl-col-<name> class.

    Sticky positioning (and any other column-specific styling) keys off
    these semantic classes instead of nth-child, so it survives column
    hiding/reordering. Regression test for issue #43.
    """
    html = _render_stock_scanner()
    for col in (*SORTABLE_COLUMNS, "buy"):
        assert f'class="wl-col wl-col-{col}' in html, (
            f"missing semantic class for {col}"
        )
    assert "nth-child" not in html


def test_stock_scanner_table_wrap_and_ticker_buy_columns_are_sticky() -> None:
    """The table wrapper scrolls both axes; Ticker/Buy stay pinned.

    One table at every breakpoint (per issue #43): the wrapper scrolls
    horizontally on narrow screens, Ticker stays pinned on the left and
    the Buy action stays pinned on the right.
    """
    html = _render_stock_scanner()
    assert 'class="tbl-wrap wl-tbl-wrap"' in html
    assert "wl-table" in html
    # #394 adds a wl-qualify band class to the Qualify-side cells.
    assert 'class="wl-col wl-col-ticker wl-qualify" data-col="ticker"' in html
    assert 'class="wl-col wl-col-buy" data-col="buy"' in html

    css = (ROOT / "app" / "api" / "static" / "css" / "watchlist.css").read_text(
        encoding="utf-8"
    )
    assert ".wl-tbl-wrap" in css
    assert "overflow-x: auto" in css
    assert "th.wl-col-ticker" in css and "td.wl-col-ticker" in css
    assert "th.wl-col-buy" in css and "td.wl-col-buy" in css
    assert "position: sticky" in css
    assert "nth-child" not in css


def test_stock_scanner_css_has_focus_visible_and_touch_target_rules() -> None:
    """Keyboard focus rings and 44px phone touch targets are present."""
    css = (ROOT / "app" / "api" / "static" / "css" / "watchlist.css").read_text(
        encoding="utf-8"
    )
    assert ":focus-visible" in css
    assert "a.ticker:focus-visible" in css
    assert ".wl-sort:focus-visible" in css
    assert "min-height: 44px" in css


def test_stock_scanner_js_persists_state_and_seeds_presets() -> None:
    js = (ROOT / "app" / "api" / "static" / "js" / "watchlist.js").read_text(
        encoding="utf-8"
    )
    assert "wl-state-v1" in js
    assert "wl-presets-v1" in js
    assert "marketNarrativeCollapsed: true" in js
    assert "state.marketNarrativeCollapsed = !state.marketNarrativeCollapsed" in js
    assert "applyMarketNarrative();" in js
    # #394 density toggle persisted in the same wl-state-v1 object.
    assert "density: 'comfortable'" in js
    assert "wl-density-toggle" in js
    assert "wl-compact" in js
    assert "htmx:afterSettle" in js
    for preset in ("Buy-ready", "UK breakouts", "Low-risk Stage 2"):
        assert preset in js, f"missing built-in preset {preset}"


def test_market_narrative_renders_accessible_collapsible_details() -> None:
    from app.schemas.market_narrative import MarketNarrative, MarketNarrativeSource

    html = _render_stock_scanner(
        market_narrative=MarketNarrative(
            headline="Breadth is improving",
            bullets=["Technology leads the current scan."],
            sources=[
                MarketNarrativeSource(
                    label="Market breadth",
                    url="https://example.com/breadth",
                )
            ],
        ),
    )

    assert 'id="market-narrative"' in html
    assert 'id="market-narrative-toggle"' in html
    # Rendered expanded server-side so the details stay reachable without JS;
    # the controller collapses on load when state.marketNarrativeCollapsed.
    assert 'aria-expanded="true"' in html
    assert 'aria-controls="market-narrative-details"' in html
    assert 'aria-label="Collapse market narrative"' in html
    assert 'id="market-narrative-details"' in html
    assert 'id="market-narrative-details" hidden' not in html
    assert "Breadth is improving" in html
    assert "Technology leads the current scan." in html


def _regime(*, is_degraded=False, spy_uptrend=True, return_52w_pct=18.4):
    from app.schemas.market_regime import MarketRegimeSnapshotV1

    return MarketRegimeSnapshotV1(
        spy_uptrend=spy_uptrend,
        return_52w_pct=return_52w_pct,
        is_degraded=is_degraded,
    )


def test_market_regime_banner_risk_on() -> None:
    html = _render_stock_scanner(market_regime=_regime(return_52w_pct=18.4))

    assert 'id="market-regime"' in html
    assert "S&amp;P 500 in confirmed uptrend" in html
    assert "#dcfce7" in html
    assert "+18.4%" in html


def test_market_regime_banner_risk_off() -> None:
    html = _render_stock_scanner(
        market_regime=_regime(spy_uptrend=False, return_52w_pct=-6.1)
    )

    assert "S&amp;P 500 below its 200-day average" in html
    assert "#fee2e2" in html
    assert "-6.1%" in html


def test_market_regime_banner_negative_zero_normalised() -> None:
    html = _render_stock_scanner(
        market_regime=_regime(spy_uptrend=False, return_52w_pct=-0.04)
    )

    assert "SPY 52-wk +0.0%" in html


def test_market_regime_banner_degraded() -> None:
    html = _render_stock_scanner(market_regime=_regime(is_degraded=True))

    assert "Market regime unavailable" in html
    assert "S&amp;P 500 in confirmed uptrend" not in html


def test_market_regime_banner_absent_without_snapshot() -> None:
    html = _render_stock_scanner(market_regime=None)

    assert 'id="market-regime"' not in html


def test_market_narrative_toggle_is_absent_without_narrative() -> None:
    html = _render_stock_scanner(market_narrative=None)

    assert 'id="market-narrative"' not in html
    assert 'id="market-narrative-toggle"' not in html
    assert 'id="market-narrative-details"' not in html


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

    # Quoted-literal form, not a bare substring: "_stock_scanner.html" (the
    # current template) legitimately contains "_scanner.html" as a
    # substring, but never as its own quoted string literal — only the
    # removed legacy filename does.
    forbidden = ('"_scanner.html"', "/partials/scanner", "python orchestrator.py")
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


def test_refresh_dropdown_exposes_three_actions_and_source_guidance() -> None:
    markup = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    toggle = markup.split('id="refresh-data-button"', 1)[1].split("</button>", 1)[0]
    menu = markup.split('class="dropdown-menu dropdown-menu-end refresh-menu', 1)[1]

    assert 'data-bs-toggle="dropdown"' in toggle
    assert 'hx-post="/refresh-data"' not in toggle
    assert 'aria-label="Refresh Data"' in toggle
    assert menu.count('hx-post="/refresh-data"') == 3
    assert "Standard refresh" in menu
    assert "Include institutional data" in menu
    assert "Custom refresh" in menu
    assert menu.count('name="extract" value="true"') == 2
    assert 'name="force_whale_wisdom"' in menu
    assert 'name="force_stocktwits"' in menu
    assert "quarterly 13F filings published after quarter end" in menu
    assert "StockTwits data is refreshed weekly" in menu
    assert "cannot make upstream data newer" in menu
    assert 'aria-label="Run standard refresh"' in menu
    assert 'aria-label="Run refresh including institutional data"' in menu


def test_only_trade_forms_activate_portfolio_after_settle() -> None:
    markup = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    assert "e.detail.elt?.getAttribute('hx-post') === '/trades'" in markup
    assert "e.detail.elt && e.detail.elt.tagName === 'FORM'" not in markup


def test_removed_scanner_partial_returns_not_found() -> None:
    response = TestClient(app).get("/partials/scanner")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_initial_and_completed_refresh_use_equivalent_stock_scanner_context() -> (
    None
):
    records = []
    trader = MagicMock()
    trader.get_portfolio.return_value = [SimpleNamespace(ticker="AAPL")]
    trader.held_tickers.return_value = {"AAPL"}
    portfolio = MagicMock()
    portfolio.load_analysis.return_value = records
    pipeline = MagicMock()
    pipeline.missing_configuration.return_value = []
    pipeline.run_once.return_value = PipelineRunResult(success=True, details="done")
    alerts = MagicMock()
    alerts.has_watching.return_value = False
    alerts.last_alerted_at.return_value = None

    initial = await partial_stock_scanner(
        _request("/partials/stock-scanner"), trader, portfolio, alerts
    )
    refreshed = await refresh_data(
        _request("/refresh-data", method="POST"),
        trader,
        portfolio,
        pipeline,
        alerts,
        confirm_missing=False,
    )

    initial_response = cast(Any, initial)
    refreshed_response = cast(Any, refreshed)
    assert (
        initial_response.template.name
        == refreshed_response.template.name
        == "_stock_scanner.html"
    )
    # Records are sorted actionable-first, so both paths return an equivalent
    # (equal) list rather than the same object as load_analysis returned.
    assert (
        initial_response.context["records"]
        == refreshed_response.context["records"]
        == records
    )
    assert (
        initial_response.context["portfolio_tickers"]
        == refreshed_response.context["portfolio_tickers"]
        == {"AAPL"}
    )
    # The post-refresh completion banner was removed (#124); the route no
    # longer injects any refresh_status context.
    assert "refresh_status" not in refreshed_response.context


@pytest.mark.asyncio
async def test_refresh_data_threads_extract_flag_to_run_once() -> None:
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

    await refresh_data(
        _request("/refresh-data", method="POST"),
        trader,
        portfolio,
        pipeline,
        alerts,
        confirm_missing=True,
        extract=True,
        force_whale_wisdom=False,
        force_stocktwits=False,
    )

    pipeline.run_once.assert_called_once_with(True, False, False)


def test_refresh_completion_banner_removed() -> None:
    """No inline post-refresh banner renders, even if refresh_status is passed (#124).

    The live /pipeline-status bar now surfaces run outcomes, so the old
    success/failure banner above the ticker table was removed.
    """
    from app.core.alerting import build_alert_ui_state
    from app.core.recommendation import classify_recommendation
    from app.services.freshness_service import calculate_freshness

    records = _sample_records()
    html = templates.get_template("_stock_scanner.html").render(
        records=records,
        portfolio_tickers=set(),
        recommendations={
            r.ticker: classify_recommendation(r, is_portfolio_holding=False)
            for r in records
        },
        alert_states={
            r.ticker: build_alert_ui_state(r, has_watching=False, last_alerted_at=None)
            for r in records
        },
        freshness=calculate_freshness(None),
        source_health=[],
        latest_attempt_error=None,
        refresh_status="Data refreshed successfully",
        refresh_success=True,
    )
    assert "Data refreshed successfully" not in html


def test_notif_badge_targets_itself_inside_bell_button() -> None:
    """The badge must not inherit the bell button's hx-target="#notif-list".

    The bell button targets #notif-list; the nested badge's outerHTML poll
    would inherit that and clobber the whole notification list. The badge
    carries an explicit hx-target="this" to swap only itself.
    """
    markup = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    badge_start = markup.index('id="notif-badge"')
    badge_tag = markup[badge_start : markup.index(">", badge_start)]
    assert 'hx-target="this"' in badge_tag


def test_dashboard_has_no_second_institutional_refresh_toggle() -> None:
    markup = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TEMPLATES.rglob("*.html"))
    )
    assert markup.count('id="refresh-institutional-button"') == 0


def test_stock_scanner_frontend_assets_are_served() -> None:
    client = TestClient(app)
    for path in (
        "/static/css/watchlist.css",
        "/static/js/watchlist.js",
        "/static/js/pipeline-refresh.js",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.text.strip(), path


def test_dashboard_cache_busts_watchlist_controller() -> None:
    markup = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    assert 'src="/static/js/watchlist.js?v=20260829-1"' in markup
    assert 'src="/static/js/watchlist.js"' not in markup


def test_stock_scanner_and_refresh_http_paths_render_canonical_partial(
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
        initial = client.get("/partials/stock-scanner")
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


def test_stock_scanner_no_longer_renders_freshness_or_source_badges(
    tmp_path, monkeypatch
) -> None:
    """Freshness/source coverage moved to the bottom status bar (#71)."""
    import app.api.stock_scanner_context as context_module

    analysis_path = tmp_path / "analysis_results.json"
    generated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    analysis_path.write_text(
        json.dumps(
            build_analysis_payload([], run_id="run-a", generated_at=generated_at)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(context_module, "ANALYSIS_JSON", analysis_path)

    repo = PipelineStatusRepository(tmp_path / "status.json")
    repo.start(run_id="run-a")
    repo.update_source_health(
        {
            SourceName.TRADINGVIEW_US: SourceHealth(
                source=SourceName.TRADINGVIEW_US,
                state=SourceState.EMPTY,
                count=0,
                display_message="No stocks matched.",
            ),
        },
        expected_run_id="run-a",
    )
    repo.finish(PipelineState.PARTIAL, expected_run_id="run-a", artifact_produced=True)
    monkeypatch.setattr(context_module, "_status_repository", repo)

    trader = MagicMock()
    trader.get_portfolio.return_value = []
    portfolio = MagicMock()
    portfolio.load_analysis.return_value = []
    alerts = MagicMock()
    alerts.has_watching.return_value = False
    alerts.last_alerted_at.return_value = None

    response = __import__("asyncio").run(
        partial_stock_scanner(
            _request("/partials/stock-scanner"), trader, portfolio, alerts
        )
    )
    markup = response.body.decode()

    assert "Last successful refresh" not in markup
    assert "data-coverage" not in markup
    assert "TradingView US" not in markup


def test_runlog_renders_structured_partial_coverage_and_legacy_fallback(
    tmp_path, monkeypatch
) -> None:
    import app.api.routes.views as views_module

    path = tmp_path / "runs.csv"
    fields = ["start", "duration_seconds", "status", "sources", "source_health_json"]
    health = SourceHealth(
        source=SourceName.TRADINGVIEW_UK,
        state=SourceState.FAILED,
        count=0,
        display_message="TradingView UK did not respond.",
    )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "start": "2026-07-18T12:00:00+00:00",
                "duration_seconds": "1",
                "status": "ok",
                "sources": "legacy source summary",
                "source_health_json": "not-json",
            }
        )
        writer.writerow(
            {
                "start": "2026-07-19T12:00:00+00:00",
                "duration_seconds": "2",
                "status": "partial",
                "source_health_json": json.dumps(
                    {"tradingview_uk": health.model_dump(mode="json")}
                ),
            }
        )
    monkeypatch.setattr(views_module, "PIPELINE_RUNS_CSV", path)

    response = __import__("asyncio").run(partial_runlog(_request("/partials/runlog")))
    markup = response.body.decode()

    assert "PARTIAL" in markup
    assert "TradingView UK" in markup
    assert "legacy source summary" in markup
    # TradingView UK is a Discovery-stage source; the funnel heading groups
    # it under a "Discovery" subheading (#108).
    assert "source-stage-label" in markup
    assert "Discovery" in markup
