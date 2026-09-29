"""Playwright tests for the AI Desk's client-side behaviour (GH-21).

A real Chromium against a live ``uvicorn`` server (the
``test_portfolio_import_queue_browser`` pattern), with the Desk service
built over ``tests.test_desk_service``'s ``tmp_path`` stack. Proves what
pytest alone cannot: selecting an item swaps the inspector without a
request and is announced, arrow keys move the selection, the URL follows
the tab and survives a reload, and no breakpoint scrolls horizontally.
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
from app.api.dependencies import get_desk_service  # noqa: E402
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
    monkeypatch.setitem(
        app.dependency_overrides, get_desk_service, lambda: desk_stack.desk()
    )
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browsers not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            tab = browser.new_page(viewport={"width": 1400, "height": 900})
            tab.goto(f"{base_url}/desk?portfolio_id={desk_stack.pid}")
            tab.locator("#desk [role=option]").first.wait_for()
            # The boot splash stays up for at least 800ms; users act after it.
            tab.locator("#boot-splash").wait_for(state="hidden")
            yield tab
        finally:
            browser.close()


def test_selecting_an_item_swaps_the_inspector_without_a_request(page) -> None:
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    last = page.locator("#desk [role=option]").last
    title = last.get_attribute("data-title")

    last.click()

    assert last.get_attribute("aria-selected") == "true"
    assert page.locator("#desk-inspector-body .desk-finding").inner_text() == title
    assert page.locator("#desk-announcer").inner_text() == f"Selected: {title}"
    assert not [u for u in requests if "/partials/desk" in u]
    # replaceState is debounced (250ms): wait for the URL to follow the click.
    page.wait_for_function(
        "id => new URLSearchParams(location.search).get('item') === id",
        arg=last.get_attribute("data-item-id"),
    )
    assert page.url.split("?")[0].endswith("/desk")


def test_arrow_keys_move_the_selection(page) -> None:
    options = page.locator("#desk [role=option]")
    options.first.focus()

    page.keyboard.press("ArrowDown")

    assert options.nth(1).get_attribute("aria-selected") == "true"
    assert options.first.get_attribute("aria-selected") == "false"
    page.keyboard.press("ArrowUp")
    assert options.first.get_attribute("aria-selected") == "true"


def test_a_reload_stays_on_the_desk_and_tabs_switch_the_url(page) -> None:
    page.locator("#desk [role=option]").last.click()
    selected = page.locator("#desk [role=option]").last.get_attribute("data-item-id")
    # The URL already carries the first item; wait for the clicked one.
    page.wait_for_function(
        "id => new URLSearchParams(location.search).get('item') === id", arg=selected
    )

    page.reload()
    page.locator("#desk [role=option]").first.wait_for()

    assert "nav-link active" in (page.locator("#tab-desk").get_attribute("class") or "")
    chosen = page.locator('#desk [role=option][aria-selected="true"]')
    assert chosen.get_attribute("data-item-id") == selected
    page.locator("#tab-runlog").click()
    page.wait_for_function("location.pathname === '/'")
    page.locator("#tab-desk").click()
    page.wait_for_function("location.pathname === '/desk'")


@pytest.mark.parametrize("width", [1400, 1240, 900, 375])
def test_no_breakpoint_scrolls_horizontally(page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})

    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )

    assert overflow <= 0
