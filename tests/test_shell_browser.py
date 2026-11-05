"""Playwright tests for the four-tab shell's navigation (GH-128, AD-3).

A real Chromium against a live ``uvicorn`` server on a free loopback port
(the ``test_portfolio_import_queue_browser`` pattern), with the Desk,
trader and portfolio services built over ``tests.test_desk_service``'s
``tmp_path`` stack. Proves what pytest alone cannot: a tab click pushes
its page URL, back/forward reloads the full shell rather than restoring a
snapshot, the badge equals Today's Needs action count, and no breakpoint
scrolls horizontally.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
import uvicorn

playwright = pytest.importorskip("playwright.sync_api")

from app.api.app import create_app  # noqa: E402
from app.api.dependencies import (  # noqa: E402
    get_desk_service,
    get_portfolio_service,
    get_trader_service,
)
from tests.test_desk_service import desk_stack  # noqa: E402, F401 -- fixture

app = create_app(strategy_jobs_enabled=False, prepare_strategy_coverage=lambda: None)


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
def page(
    base_url: str,
    desk_stack: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[object]:
    overrides = {
        get_desk_service: lambda: desk_stack.desk(),
        get_trader_service: lambda: desk_stack.trader,
        get_portfolio_service: lambda: desk_stack.service,
    }
    for dependency, override in overrides.items():
        monkeypatch.setitem(app.dependency_overrides, dependency, override)
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browsers not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            tab = browser.new_page(viewport={"width": 1400, "height": 900})
            tab.goto(f"{base_url}/today?portfolio_id={desk_stack.pid}")
            tab.locator("#today").wait_for()
            tab.locator("#boot-splash").wait_for(state="hidden")
            yield tab
        finally:
            browser.close()


def test_the_badge_equals_todays_needs_action_count(page) -> None:
    page.locator("#notif-badge.has-count").wait_for()

    rows = page.locator("#today .today-act-row").count()

    assert page.locator("#notif-badge [aria-hidden]").inner_text() == str(rows)


def test_a_tab_pushes_its_url_and_back_reloads_the_shell(page) -> None:
    page.locator("#tab-portfolio").click()
    page.wait_for_function("location.pathname === '/portfolio'")
    assert "active" in (page.locator("#tab-portfolio").get_attribute("class") or "")

    page.go_back()
    page.wait_for_function("location.pathname === '/today'")
    page.locator("#today").wait_for()

    assert "active" in (page.locator("#tab-today").get_attribute("class") or "")
    assert "active" not in (page.locator("#tab-portfolio").get_attribute("class") or "")
    # No snapshot was stored for back/forward to restore.
    assert page.evaluate("localStorage.getItem('htmx-history-cache')") in (None, "[]")
    page.go_forward()
    page.wait_for_function("location.pathname === '/portfolio'")
    assert "active" in (page.locator("#tab-portfolio").get_attribute("class") or "")


def test_the_url_portfolio_wins_and_is_remembered(page, desk_stack) -> None:  # noqa: F811
    assert page.evaluate("activePortfolio()") == str(desk_stack.pid)
    assert page.evaluate("localStorage.getItem('activePortfolioId')") == str(
        desk_stack.pid
    )


@pytest.mark.parametrize("width", [1400, 900, 375])
def test_no_breakpoint_scrolls_horizontally(page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})

    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )

    assert overflow <= 0
