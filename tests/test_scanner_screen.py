"""Contract tests for the simplified Scanner screen (GH-129).

AD-6 (the Buy-signals chip counts what Today counts), AD-10 (the context,
not the template, decides the Watching group and its order) and the
one-title-line / one-toolbar layout. Records are built in memory and the
Desk stack runs over ``tests.test_desk_service``'s ``tmp_path`` stores, so no
real artifact or database is opened.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.api.stock_scanner_context as context_module
from app.api.app import app
from app.api.dependencies import (
    get_alerts_repository,
    get_portfolio_service,
    get_trader_service,
)
from app.api.stock_scanner_context import build_stock_scanner_context
from app.repositories.alerts_repo import AlertReadState
from app.schemas.record import StockRecord
from tests.test_desk_service import desk_stack  # noqa: F401 -- fixture
from tests.test_stock_scanner_row_weight import _record

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app" / "api" / "static" / "js" / "watchlist.js").read_text("utf-8")
#: An hour ago, so both the 24h breakout and 7-day stay-alert cooldowns hold.
ALERTED = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=1)


def _eligible(hours: int) -> str:
    return (ALERTED + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


def _with(i: int, ticker: str, **analysis: object) -> StockRecord:
    """Record ``i`` of the row-weight fixture, renamed and re-analysed."""
    base = _record(i)
    assert base.analysis is not None
    updated = base.analysis.model_copy(
        update={"fresh_breakout": False, "multiyear_breakout": False, **analysis}
    )
    return base.model_copy(update={"ticker": ticker, "analysis": updated})


def _mix() -> list[StockRecord]:
    """Buys (one held, one low volume), stay-alerts and a Watch row."""
    buy = {"stage": "Stage 2", "entry_zone": "broken_out", "score": 8}
    near = {"stage": "Stage 2", "entry_zone": "approaching", "score": 6}
    return [
        _with(1, "WATCH1", stage="Stage 2", entry_zone="getting_close", score=6),
        _with(2, "NEAR1", **near),  # email-eligible, not yet alerted
        _with(3, "ALERTED1", **near),
        _with(4, "BUY1", **buy, volume_confirmed=True),
        _with(5, "LOWVOL1", **buy, volume_confirmed=False),
        _with(6, "ALERTED2", **{**near, "score": 9}),
        _with(7, "AAA", **buy, volume_confirmed=True),  # held in the stack
        # A fresh VCP buy: email-eligible, so it can be on cooldown.
        _with(8, "ALERTEDBUY", **buy, volume_confirmed=True, fresh_breakout=True),
    ]


def _alerts() -> MagicMock:
    alerts = MagicMock()
    alerts.states_for_tickers.return_value = {
        ticker: AlertReadState(has_watching=False, last_alerted_at=ALERTED.isoformat())
        for ticker in ("ALERTED1", "ALERTED2", "ALERTEDBUY")
    }
    return alerts


@pytest.fixture
def scanner(desk_stack, monkeypatch) -> SimpleNamespace:  # noqa: F811
    """The real scanner route over the Desk stack, with the mixed records."""
    monkeypatch.setattr(desk_stack.service, "load_analysis", _mix)
    monkeypatch.setattr(context_module, "load_market_narrative", lambda: None)
    monkeypatch.setattr(context_module, "load_market_regime", lambda: None)
    alerts = _alerts()
    overrides = {
        get_trader_service: lambda: desk_stack.trader,
        get_portfolio_service: lambda: desk_stack.service,
        get_alerts_repository: lambda: alerts,
    }
    for dependency, override in overrides.items():
        monkeypatch.setitem(app.dependency_overrides, dependency, override)
    html = TestClient(app).get("/partials/scanner").text
    context = build_stock_scanner_context(desk_stack.trader, desk_stack.service, alerts)
    return SimpleNamespace(stack=desk_stack, html=html, context=context)


def _row(html: str, ticker: str) -> str:
    match = re.search(rf'<tr [^>]*><td data-val="{ticker}">.*?</tr>', html)
    assert match is not None, ticker
    return match[0]


def test_buy_chip_count_equals_todays_buy_signals(scanner) -> None:
    today = scanner.stack.desk().today_items(scanner.stack.pid)

    assert today.buy_signal_count == 4
    assert scanner.context["buy_signal_count"] == today.buy_signal_count
    assert "Buy signals · 4</button>" in scanner.html
    assert "<strong>4 buy signals</strong>" in scanner.html
    assert scanner.html.count('data-rec="buy"') == 4


def test_already_alerted_rows_form_the_watching_group(scanner) -> None:
    order = [r.ticker for r in scanner.context["records"]]

    assert scanner.context["watching"] == {"ALERTED1", "ALERTED2"}
    # Buys first, then Stay Alert, then the Watching run, then Watch.
    assert order.index("NEAR1") < order.index("ALERTED2") < order.index("ALERTED1")
    assert order.index("ALERTED1") < order.index("WATCH1")
    assert max(order.index(t) for t in ("BUY1", "LOWVOL1", "AAA", "ALERTEDBUY")) < (
        order.index("NEAR1")
    )
    row = _row(scanner.html, "ALERTED1")
    assert 'data-watch="true"' in row
    tip = f'data-tip="cooldown:{_eligible(24 * 7)}"'
    assert f'<span class="rec rec-watching" {tip}>Watching<' in row
    assert "already alerted" not in scanner.html  # no repeated sub-line


def test_an_alerted_buy_keeps_its_buy_label(scanner) -> None:
    row = _row(scanner.html, "ALERTEDBUY")

    assert "data-watch" not in row
    tip = f'data-tip="cooldown:{_eligible(24)}"'
    assert f'<span class="rec rec-buy" {tip}>Buy<' in row
    near = _row(scanner.html, "NEAR1")
    assert '<span class="rec rec-alert" data-tip="mail">Stay Alert<' in near
    low = _row(scanner.html, "LOWVOL1")
    assert '<span class="rec rec-lowvol">Buy · low vol<' in low


def test_one_title_line_one_toolbar_and_no_banner(scanner) -> None:
    html = scanner.html
    above = html[: html.index('<div class="tbl-wrap')]

    assert above.count("<h1") == 1 and '<h1 tabindex="-1">Scanner</h1>' in above
    assert "8 stocks · " in above
    assert above.count('role="toolbar"') == 1
    assert 'class="alert' not in above
    chips = re.findall(r'<button class="btn-filter"[^>]*>([^<]+)</button>', above)
    assert chips == [
        "All",
        "Buy signals · 4",
        "Fresh VCP",
        "Base breakout",
        "SEPA 8/8",
        "Near entry",
        "UK",
    ]
    assert '<th class="wl-col wl-col-rec" data-col="rec">' in html
    assert ">Signal<" in html


def test_saved_view_keys_keep_their_names_and_new_defaults_migrate() -> None:
    assert "const STATE_KEY = 'wl-state-v1';" in JS
    assert "const PRESET_KEY = 'wl-presets-v1';" in JS
    assert "buy: (row) => row.dataset.rec === 'buy'" in JS
    for key in ("sepa8", "alert", "breakout", "myb", "uk"):
        assert f"    {key}: (row) =>" in JS, key
    hidden = re.search(r"const DEFAULT_HIDDEN = \[([^\]]*)\]", JS)
    assert hidden is not None
    hidden_cols = {x.strip() for x in hidden[1].split(",")}
    assert {"'congress'", "'funds'", "'targets'"} <= hidden_cols
    # A pre-#129 saved state gets compact rows and the new hidden columns once.
    assert "if (!(saved.layout >= LAYOUT)) {" in JS
    assert "const LAYOUT_HIDDEN = ['congress', 'funds', 'targets'];" in JS
    assert "density: 'compact'," in JS


def test_only_alerted_stay_alerts_join_the_watching_group() -> None:
    """A suppressed Avoid/Extended row keeps its own label and rank."""
    from app.api.stock_scanner_context import watching_tickers
    from app.core.alerting import AlertUiState
    from app.core.recommendation import classify_recommendation

    records = {
        "NEAR": _with(1, "NEAR", stage="Stage 2", entry_zone="approaching", score=6),
        "AVOID": _with(2, "AVOID", stage="Stage 4", score=2, fresh_breakout=True),
        "EXT": _with(3, "EXT", stage="Stage 2", entry_zone="extended", score=8),
    }
    recs = {t: classify_recommendation(r) for t, r in records.items()}
    alerted = AlertUiState(True, "label", True, None)

    assert watching_tickers(recs, dict.fromkeys(records, alerted)) == {"NEAR"}
