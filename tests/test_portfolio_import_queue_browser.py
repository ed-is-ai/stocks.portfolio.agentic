"""Playwright behavioral tests for the multi-file SIPP import queue's
portfolio-selector lock/discard (#210, Story 1.3 Gate 2, AC3/AC4).

pytest alone cannot execute JavaScript, so it can only assert on rendered
HTML/JS *source text* (see ``tests/test_portfolio_import.py``'s
``test_rendered_portfolio_partial_includes_portfolio_select_element`` for
that structural half). Proving the selector is actually inert while a fetch
is in flight, and that a discarded response is actually never rendered,
requires a real browser executing real JavaScript against a real DOM with
real timing -- nothing else in this repo's suite does that today. See the
story's Dev Notes "No JS Test Harness Exists" for the full rationale.

This module drives a real Chromium instance (via Playwright) against a real
``uvicorn`` server started on a background thread for the module, with the
same kind of dependency overrides used elsewhere in this suite (a mocked
``TraderService`` wrapped by a *real* ``PortfolioService`` so the actual
Jinja templates render) -- hermetic, no real database or SIPP CSV touched.

Scope: exactly the two behaviors named in Gate 2's last task --
  (a) ``#portfolioSelect`` is disabled for the duration of an in-flight
      import queue, and re-enabled once it finishes.
  (b) a response for an import whose captured portfolio ID no longer
      matches the active view is discarded -- never written into
      ``#tab-content``.

Scoping note on (b): the portfolio switch is triggered directly via
``setActivePortfolio`` (the same function the Delete/Rename/Create-portfolio
forms *and* a second browser tab all call) rather than by driving the
Delete-portfolio button's Bootstrap dropdown + native ``confirm()`` dialog.
The discard guard's contract (``activePortfolio() !== portfolioId``) is
agnostic to *which* of those paths changed the value, so this is a faithful,
hermetic proof of the guard itself -- and it is also a literal, direct proof
of the second-tab case (nothing in *this* tab's ``#tab-content`` is touched
by whatever changed ``localStorage``), which the Delete-button route would
not exercise any more directly. Driving the real dropdown/confirm() chrome
would pull in unrelated CDN-script-driven UI mechanics this story does not
touch, for no added proof of Gate 2's own logic.

Requires Chromium browser binaries (``uv run playwright install chromium``)
-- not covered by ``uv sync`` alone. Skips itself if Playwright's browsers
aren't installed, rather than failing the whole suite in an environment
where they aren't (e.g. a minimal CI image).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import uvicorn
from playwright.sync_api import Browser, Dialog, Page, expect, sync_playwright

from app.api.app import app
from app.api.dependencies import (
    get_notifications_repository,
    get_portfolio_service,
    get_trader_service,
)
from app.schemas.trade import Portfolio, SippImportResult
from app.services.portfolio_service import PortfolioService

# How long the mocked import "processing" takes server-side. Long enough
# that Playwright's assertions can reliably observe the mid-flight disabled
# state before it resolves, short enough the module stays fast.
_IMPORT_DELAY_S = 0.6


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    """Run the real FastAPI app on a background thread for this module.

    No existing fixture in this repo starts a live server (#210's Dev Notes
    confirm this directly) -- ``uvicorn.Server`` bound to a free loopback
    port, run inside a daemon thread, is the smallest way to get one.
    """
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        # Inside the try so a startup timeout still runs the finally below --
        # otherwise the thread and bound port are never cleaned up.
        assert server.started, "live test server failed to start within 10s"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def mocked_trader() -> Iterator[MagicMock]:
    """Hermetic dependency overrides: a mocked ``TraderService`` wrapped by
    a real ``PortfolioService`` (so the actual Jinja partials render) --
    same pattern as ``tests/test_portfolio_import.py``'s
    ``test_rendered_portfolio_partial_includes_portfolio_select_element``,
    reused here because the live server must serve genuine markup for a
    real browser to interact with."""
    mock_trader = MagicMock()
    mock_trader.portfolio_exists.return_value = True
    mock_trader.list_portfolios.return_value = [
        Portfolio(id=1, name="SIPP", created_at="2024-01-01")
    ]
    mock_trader.load_price_cache.return_value = ({}, None, {})
    mock_trader.get_portfolio.return_value = []
    mock_trader.get_cash_balance.return_value = 0.0
    mock_trader.get_cash_flows.return_value = []
    mock_trader.snapshot_history.return_value = []
    mock_trader.get_portfolio_meta.return_value = None

    def _slow_successful_import(
        content: bytes, portfolio_id: int | None
    ) -> SippImportResult:
        # Simulates real processing time so the queue's fetch is reliably
        # still in flight when the test asserts the mid-import disabled
        # state, before the response resolves and re-enables it.
        time.sleep(_IMPORT_DELAY_S)
        return SippImportResult(cash_balance=1000.0, buy_count=1, sell_count=0)

    mock_trader.import_sipp.side_effect = _slow_successful_import

    mock_portfolio_service = PortfolioService(mock_trader)
    # The bell icon in index.html polls the notification-centre badge/panel
    # routes in the background (unrelated to this story) -- these must
    # return real values, not an unconfigured MagicMock, or that unrelated
    # polling request fails mid-test and can pollute results.
    mock_notifications = MagicMock()
    mock_notifications.unread_count.return_value = 0
    mock_notifications.recent.return_value = []
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio_service
    app.dependency_overrides[get_notifications_repository] = lambda: mock_notifications
    try:
        yield mock_trader
    finally:
        app.dependency_overrides.pop(get_trader_service, None)
        app.dependency_overrides.pop(get_portfolio_service, None)
        app.dependency_overrides.pop(get_notifications_repository, None)


@pytest.fixture
def mocked_trader_mixed_results() -> Iterator[MagicMock]:
    """Same as ``mocked_trader``, but the first queued upload succeeds and
    the second reports a row-count mismatch (``status="error"``) -- the
    exact multi-file mix the AC1/AC2 fix in ``handleSippImportSubmit``
    (surfacing a mismatch's counts instead of folding it into ``failures``)
    must handle correctly."""
    mock_trader = MagicMock()
    mock_trader.portfolio_exists.return_value = True
    mock_trader.list_portfolios.return_value = [
        Portfolio(id=1, name="SIPP", created_at="2024-01-01")
    ]
    mock_trader.load_price_cache.return_value = ({}, None, {})
    mock_trader.get_portfolio.return_value = []
    mock_trader.get_cash_balance.return_value = 0.0
    mock_trader.get_cash_flows.return_value = []
    mock_trader.snapshot_history.return_value = []
    mock_trader.get_portfolio_meta.return_value = None

    results = [
        SippImportResult(cash_balance=1000.0, buy_count=1, sell_count=0, status="ok"),
        SippImportResult(
            cash_balance=1000.0,
            buy_count=2,
            sell_count=0,
            cash_flow_count=1,
            total_rows=4,
            status="error",
        ),
    ]

    def _mixed_import(content: bytes, portfolio_id: int | None) -> SippImportResult:
        return results.pop(0)

    mock_trader.import_sipp.side_effect = _mixed_import

    mock_portfolio_service = PortfolioService(mock_trader)
    mock_notifications = MagicMock()
    mock_notifications.unread_count.return_value = 0
    mock_notifications.recent.return_value = []
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio_service
    app.dependency_overrides[get_notifications_repository] = lambda: mock_notifications
    try:
        yield mock_trader
    finally:
        app.dependency_overrides.pop(get_trader_service, None)
        app.dependency_overrides.pop(get_portfolio_service, None)
        app.dependency_overrides.pop(get_notifications_repository, None)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    """A single Chromium instance shared by this module's tests.

    ``pytest-playwright``'s ``page``/``browser`` fixtures aren't installed
    in this repo (only the ``playwright`` package itself, used elsewhere
    for scraping) -- driving ``sync_playwright()`` directly here is the
    smallest addition that doesn't pull in a new pytest plugin dependency.
    Skips this module (rather than erroring) if Chromium's browser binary
    isn't installed locally -- a one-time ``uv run playwright install
    chromium`` setup step this story adds as a new *use* of the pinned
    dependency, not covered by ``uv sync`` alone.
    """
    with sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium not available for Playwright: {exc}")
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    """A fresh browser page (and isolated ``localStorage``) per test."""
    browser_page = browser.new_page()
    try:
        yield browser_page
    finally:
        browser_page.close()


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    path = tmp_path / "q1_2024.csv"
    path.write_text(
        "Date,Symbol,Quantity,Price,Running Balance\n01/01/2024,AAPL,10,100,5000\n",
        encoding="utf-8",
    )
    return path


def _load_portfolio_tab(page: Page, base_url: str) -> None:
    page.goto(base_url + "/")
    page.click("#tab-portfolio")
    expect(page.locator("#portfolioSelect")).to_be_visible()


def _open_import_dropdown(page: Page) -> None:
    # The import form lives inside the "Add holding" dropdown, which
    # Bootstrap keeps display:none until its toggle button is clicked.
    page.click("button:has-text('Add holding')")
    expect(page.locator("input[type=file]")).to_be_attached()


def _capture_dialogs(page: Page) -> list[str]:
    """Register a handler that dismisses every ``alert()``/``confirm()``
    and records its message, returning the list that fills in as dialogs
    fire. Without a handler Playwright auto-dismisses dialogs silently,
    which is enough for tests that don't care what the alert said -- this
    is for the ones that do."""
    messages: list[str] = []

    def _on_dialog(dialog: Dialog) -> None:
        messages.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _on_dialog)
    return messages


def test_selector_disabled_during_queue_then_reenabled(
    base_url: str, mocked_trader: MagicMock, sample_csv: Path, page: Page
) -> None:
    """AC3: the account selector is inert for the duration of an in-flight
    import queue, then usable again once it finishes."""
    _load_portfolio_tab(page, base_url)
    _open_import_dropdown(page)

    page.set_input_files("input[type=file]", str(sample_csv))
    page.click("button:has-text('Import CSV')")

    # Disabled almost immediately -- well before the mocked server-side
    # delay elapses.
    expect(page.locator("#portfolioSelect")).to_be_disabled(timeout=2000)

    # Once the queue's single request resolves, the successful import's
    # response replaces #tab-content wholesale with a freshly rendered,
    # non-disabled selector (the normal-completion half of AC3's "then
    # false again" -- see index.html's early-return-branch comment for the
    # other half, the offline/every-file-failed path, which this test does
    # not exercise).
    expect(page.locator("#portfolioSelect")).to_be_enabled(
        timeout=int((_IMPORT_DELAY_S + 4) * 1000)
    )


def test_response_for_switched_away_portfolio_is_discarded(
    base_url: str, mocked_trader: MagicMock, sample_csv: Path, page: Page
) -> None:
    """AC4: once the active portfolio changes mid-flight (here, simulated
    directly via ``setActivePortfolio`` -- see module docstring for why),
    the queued import's eventual response must never be written into
    #tab-content, and must never be treated as applying to the portfolio
    that is now active. Also covers the discard branch's own re-enable of
    the selector/button/file-input: since nothing else touches this tab's
    #tab-content in this scenario (unlike the delete/create-portfolio
    paths), the discard branch is the only place that can undo the
    queue-start disable -- if it didn't, these controls would stay
    disabled forever. Also proves the discard alert reports the file
    that already imported before the switch -- a discard that hard-fails
    zero files must not go silent just because nothing failed (#210
    follow-up review)."""
    dialog_messages = _capture_dialogs(page)

    _load_portfolio_tab(page, base_url)
    _open_import_dropdown(page)

    page.set_input_files("input[type=file]", str(sample_csv))
    page.click("button:has-text('Import CSV')")

    # Confirms the request is genuinely in flight (not already resolved)
    # before switching the active portfolio out from under it.
    expect(page.locator("#portfolioSelect")).to_be_disabled(timeout=2000)
    page.evaluate("window.setActivePortfolio('999')")

    # Deterministic completion signal rather than a blind sleep: the
    # selector only re-enables once the queue's fetch has resolved and
    # either the discard guard or the normal completion path has run --
    # whichever it is, by the time this succeeds the response has been
    # fully handled one way or the other, so the assertion below can't
    # pass vacuously by checking before the response ever arrived.
    expect(page.locator("#portfolioSelect")).to_be_enabled(
        timeout=int((_IMPORT_DELAY_S + 4) * 1000)
    )

    # The success banner (data-import-buy-count is only ever present on a
    # successful import response) never landed in #tab-content.
    expect(page.locator("#tab-content [data-import-buy-count]")).to_have_count(0)
    # The write already happened server-side against the portfolio this
    # queue was locked to -- the discard alert must say so.
    assert any("1 file(s) were already imported" in m for m in dialog_messages)


def test_discard_reports_files_already_imported_mid_batch_switch(
    base_url: str, mocked_trader: MagicMock, tmp_path: Path, page: Page
) -> None:
    """AC4, multi-file case: switching portfolios after the first file in a
    two-file queue has already completed -- and its write already landed
    under the locked ``portfolio_id`` -- but before the second file
    resolves. Distinct from the single-file discard test above, where the
    switch happens before any response arrives at all: here, a real write
    has already happened by the time the switch fires, which is the
    scenario the follow-up review found had no coverage. The discard
    alert must report both files as already-imported once the queue
    finishes (the loop runs the second file to completion regardless of
    the switch -- only the render is discarded, not the in-flight
    request)."""
    csv_a = tmp_path / "a.csv"
    csv_a.write_text(
        "Date,Symbol,Quantity,Price,Running Balance\n01/01/2024,AAPL,10,100,5000\n",
        encoding="utf-8",
    )
    csv_b = tmp_path / "b.csv"
    csv_b.write_text(
        "Date,Symbol,Quantity,Price,Running Balance\n02/01/2024,MSFT,5,200,4000\n",
        encoding="utf-8",
    )
    dialog_messages = _capture_dialogs(page)

    _load_portfolio_tab(page, base_url)
    _open_import_dropdown(page)

    page.set_input_files("input[type=file]", [str(csv_a), str(csv_b)])
    page.click("button:has-text('Import CSV')")

    # Switch after the first file's roundtrip has completed (~_IMPORT_DELAY_S)
    # but before the sequential loop reaches the second file's own
    # completion (~2x _IMPORT_DELAY_S) -- the window where a write has
    # already happened but the queue is still in flight.
    page.wait_for_timeout(int((_IMPORT_DELAY_S + 0.2) * 1000))
    page.evaluate("window.setActivePortfolio('999')")

    expect(page.locator("#portfolioSelect")).to_be_enabled(
        timeout=int((_IMPORT_DELAY_S * 2 + 4) * 1000)
    )

    expect(page.locator("#tab-content [data-import-buy-count]")).to_have_count(0)
    assert any("2 file(s) were already imported" in m for m in dialog_messages)


def test_multi_file_summary_flags_mismatch_without_losing_counts(
    base_url: str,
    mocked_trader_mixed_results: MagicMock,
    tmp_path: Path,
    page: Page,
) -> None:
    """AC1/AC2 regression, multi-file case: a queued batch of one ok file
    and one status="error" (row-count mismatch) file must report the
    mismatched file's buy/sell/cash counts with a "ROW COUNT MISMATCH" flag
    -- not fold it into the generic failures list and lose that detail --
    while "Imported X of Y" still excludes it from the success count."""
    csv_a = tmp_path / "a.csv"
    csv_a.write_text(
        "Date,Symbol,Quantity,Price,Running Balance\n01/01/2024,AAPL,10,100,5000\n",
        encoding="utf-8",
    )
    csv_b = tmp_path / "b.csv"
    csv_b.write_text(
        "Date,Symbol,Quantity,Price,Running Balance\n02/01/2024,MSFT,5,200,4000\n",
        encoding="utf-8",
    )

    _load_portfolio_tab(page, base_url)
    _open_import_dropdown(page)

    page.set_input_files("input[type=file]", [str(csv_a), str(csv_b)])
    page.click("button:has-text('Import CSV')")

    summary = page.locator("#tab-content [data-import-buy-count]")
    expect(summary).to_be_visible(timeout=int((_IMPORT_DELAY_S * 2 + 4) * 1000))

    summary_text = summary.inner_text()
    assert "Imported 1 of 2 file(s)" in summary_text
    assert "ROW COUNT MISMATCH" in summary_text
    assert "b.csv" in summary_text
    # The mismatched file's counts (2 buys, 1 cash txn) must still be
    # visible -- not collapsed into a bare "b.csv: ... — failed" line.
    assert "2 buy(s)" in summary_text
    assert "1 cash txn(s)" in summary_text
