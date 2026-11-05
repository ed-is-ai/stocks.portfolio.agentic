"""Playwright tests for the simplified Scanner screen (GH-129).

A real Chromium at 1440×900 against a live ``uvicorn`` server on a free
loopback port (the ``test_shell_browser`` pattern), with the scanner fed 120
in-memory records over ``tests.test_desk_service``'s ``tmp_path`` stack.
Proves what markup tests cannot: about 15 rows fit above the fold, for a new
viewer and for one with a saved state from before #129; row actions show on
keyboard focus; "Show next 50" and the Buy-signals chip work client-side.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import uvicorn

playwright = pytest.importorskip("playwright.sync_api")

import app.api.stock_scanner_context as context_module  # noqa: E402
from app.api.app import create_app  # noqa: E402
from app.api.dependencies import (  # noqa: E402
    get_alerts_repository,
    get_desk_service,
    get_portfolio_service,
    get_trader_service,
)
from tests.test_desk_service import desk_stack  # noqa: E402, F401 -- fixture
from tests.test_scanner_screen import _with  # noqa: E402
from tests.test_stock_scanner_row_weight import _record  # noqa: E402
from tests.test_views_index_freshness import _use_artifact  # noqa: E402

app = create_app(strategy_jobs_enabled=False, prepare_strategy_coverage=lambda: None)
ROW_COUNT = 120
BUYS = 3
#: What a viewer saved before #129: comfortable rows, Congress/Funds/Targets on.
LEGACY_STATE = {
    "query": "",
    "filters": [],
    "ranges": {},
    "sortKeys": [],
    "hidden": ["canslim", "momentum", "vsstop"],
    "presetId": None,
    "marketNarrativeCollapsed": True,
    "density": "comfortable",
}
VISIBLE_ROWS = """() => {
  const wrap = document.querySelector('.wl-tbl-wrap').getBoundingClientRect();
  const bottom = Math.min(wrap.bottom, window.innerHeight);
  return [...document.querySelectorAll('#watchlist-table tbody tr[data-sepa]')]
    .filter((r) => !r.hidden && r.getBoundingClientRect().bottom <= bottom).length;
}"""


def _records() -> list:
    buy = {"stage": "Stage 2", "entry_zone": "broken_out", "score": 8}
    buys = [_with(i, f"BUY{i}", **buy, volume_confirmed=True) for i in range(BUYS)]
    return buys + [_record(i) for i in range(BUYS, ROW_COUNT)]


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    """Run the app on a free loopback port in a daemon thread."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert server.started, "live test server failed to start within 10s"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def browser(
    base_url: str,
    desk_stack: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> Iterator[object]:
    _use_artifact(monkeypatch, tmp_path, datetime.now(UTC))
    monkeypatch.setattr(desk_stack.service, "load_analysis", _records)
    monkeypatch.setattr(context_module, "load_market_narrative", lambda: None)
    monkeypatch.setattr(context_module, "load_market_regime", lambda: None)
    alerts = MagicMock()
    alerts.states_for_tickers.return_value = {}
    overrides = {
        get_desk_service: lambda: desk_stack.desk(),
        get_trader_service: lambda: desk_stack.trader,
        get_portfolio_service: lambda: desk_stack.service,
        get_alerts_repository: lambda: alerts,
    }
    for dependency, override in overrides.items():
        monkeypatch.setitem(app.dependency_overrides, dependency, override)
    with playwright.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as exc:  # browsers not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            yield SimpleNamespace(chromium=chromium, url=f"{base_url}/scanner")
        finally:
            chromium.close()


def _open(browser, saved: dict | None = None):
    tab = browser.chromium.new_page(viewport={"width": 1440, "height": 900})
    if saved is not None:
        tab.add_init_script(
            f"localStorage.setItem('wl-state-v1', {json.dumps(json.dumps(saved))})"
        )
    tab.goto(browser.url)
    tab.locator("#watchlist-table tbody tr").first.wait_for()
    tab.locator("#boot-splash").wait_for(state="hidden")
    tab.wait_for_function("document.getElementById('wl-match-count')")
    return tab


@pytest.mark.parametrize("saved", [None, LEGACY_STATE], ids=["new", "pre-129"])
def test_about_fifteen_rows_fit_above_the_fold(browser, saved) -> None:
    tab = _open(browser, saved)

    assert tab.evaluate(VISIBLE_ROWS) >= 15
    assert tab.locator("#watchlist-table.wl-compact").count() == 1
    for col in ("congress", "funds", "targets"):
        assert tab.locator(f'th[data-col="{col}"]').is_hidden(), col
    state = json.loads(tab.evaluate("localStorage.getItem('wl-state-v1')"))
    assert state["layout"] == 2 and state["density"] == "compact"


def test_row_actions_show_on_keyboard_focus(browser) -> None:
    tab = _open(browser)
    buy = tab.locator("#watchlist-table tbody tr[data-sepa] .btn-buy-sm").first

    assert buy.evaluate("b => getComputedStyle(b).opacity") == "0"
    buy.focus()
    assert buy.evaluate("b => getComputedStyle(b).opacity") == "1"
    why = tab.locator("#watchlist-table tbody tr[data-sepa] .wl-why").first
    assert why.evaluate("b => getComputedStyle(b).opacity") == "1"


def test_show_next_and_the_buy_chip_work_client_side(browser) -> None:
    tab = _open(browser)
    shown = "#watchlist-table tbody tr[data-sepa]:not([hidden])"
    count = tab.locator("#wl-match-count")

    assert tab.locator(shown).count() == 50
    assert count.inner_text().startswith(f"Showing 50 of {ROW_COUNT}")
    tab.get_by_role("button", name="Show next 50").click()
    assert tab.locator(shown).count() == 100
    tab.locator('#wl-toolbar [data-filter="buy"]').click()
    assert tab.locator(shown).count() == BUYS
    assert count.inner_text().startswith(f"Showing {BUYS} of {BUYS}")
    assert tab.locator("#wl-filter-all").get_attribute("aria-pressed") == "false"
    tab.locator("#wl-filter-all").click()
    assert tab.locator(shown).count() == 50
