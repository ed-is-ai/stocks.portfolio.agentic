"""Tests for the POST /import-sipp portfolio CSV upload route (#145)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_notifications_repository,
    get_portfolio_service,
    get_trader_service,
)
from app.api.templating import templates
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.trade import Portfolio, SippImportResult
from app.services.portfolio_service import PortfolioService

client = TestClient(app)

_CSV = b"Date,Symbol,Quantity,Price,Running Balance\n01/01/2024,AAPL,10,100,5000\n"


@pytest.fixture
def mocked_import(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: HTMLResponse("ok", status_code=k.get("status_code", 200)),
    )
    monkeypatch.setattr(
        "app.api.routes.portfolio.IMPORTED_FILES_DIR", tmp_path / "imported"
    )
    mock_trader = MagicMock()
    mock_trader.portfolio_exists.return_value = True
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0, buy_count=2, sell_count=0, cash_flow_count=1
    )
    mock_trader.get_portfolio.return_value = [object(), object()]
    mock_trader.get_portfolio_meta.return_value = None
    mock_portfolio = MagicMock()
    mock_portfolio.default_portfolio_context.return_value = {}
    mock_portfolio.portfolio_partial_context.return_value = {}
    mock_notifications = MagicMock()
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    app.dependency_overrides[get_notifications_repository] = lambda: mock_notifications
    try:
        yield mock_trader, mock_portfolio, mock_notifications, tmp_path
    finally:
        app.dependency_overrides.clear()


def _upload(
    filename: str = "merged.csv",
    content: bytes = _CSV,
    token: str = "s3cret",
    portfolio_id: int = 1,
):
    headers = {"X-Auth-Token": token} if token else {}
    return client.post(
        "/import-sipp",
        files={"file": (filename, content, "text/csv")},
        data={"portfolio_id": str(portfolio_id)},
        headers=headers,
    )


def test_happy_path_passes_uploaded_bytes_directly(mocked_import):
    mock_trader, _, _, tmp_path = mocked_import
    resp = _upload()

    assert resp.status_code == 200
    # import_sipp is called once with the raw uploaded bytes, not a saved
    # path — no shared filesystem location is written or read (#210).
    mock_trader.import_sipp.assert_called_once()
    assert mock_trader.import_sipp.call_args.args[0] == _CSV
    assert not (tmp_path / "SIPP").exists()
    mock_trader.get_portfolio.assert_called_once()


def test_happy_path_archives_a_copy_of_the_uploaded_file(mocked_import):
    """A successful import must leave a permanent, timestamped copy under
    data/imported/ — the only on-disk trace of the upload, since the CSV is
    parsed directly from its own request-owned bytes rather than a shared
    working copy (#210)."""
    _, _, _, tmp_path = mocked_import
    resp = _upload(filename="q1_2024.csv")

    assert resp.status_code == 200
    archived = list((tmp_path / "imported").glob("*_q1_2024.csv"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == _CSV


def test_repeated_imports_of_the_same_filename_are_both_archived(mocked_import):
    """Two imports of a same-named file must not clobber each other's
    archive copy — each timestamped filename is distinct."""
    _, _, _, tmp_path = mocked_import
    _upload(filename="merged.csv", content=b"a,b\n1,2\n")
    _upload(filename="merged.csv", content=b"a,b\n3,4\n")

    archived = sorted((tmp_path / "imported").glob("*_merged.csv"))
    assert len(archived) == 2
    assert {p.read_bytes() for p in archived} == {b"a,b\n1,2\n", b"a,b\n3,4\n"}


def test_failed_import_is_still_archived(mocked_import):
    """A file that fails to import still needs to be inspectable to diagnose
    why, so it's archived exactly like a successful one."""
    mock_trader, _, _, tmp_path = mocked_import
    mock_trader.import_sipp.side_effect = ValueError("bad row")
    resp = _upload(filename="bad.csv")

    assert resp.status_code == 400
    archived = list((tmp_path / "imported").glob("*_bad.csv"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == _CSV


def test_rejected_non_csv_upload_is_still_archived(mocked_import):
    """Even a wrong-extension upload — rejected before any import is
    attempted — is archived, since the point is a record of every upload
    attempt, not just ones that got as far as parsing."""
    _, _, _, tmp_path = mocked_import
    resp = _upload(filename="portfolio.txt")

    assert resp.status_code == 400
    archived = list((tmp_path / "imported").glob("*_portfolio.txt"))
    assert len(archived) == 1


def test_missing_portfolio_upload_is_still_archived(mocked_import):
    """The earliest rejection point — no valid portfolio selected, before
    the file is even parsed — still archives the upload."""
    mock_trader, _, _, tmp_path = mocked_import
    mock_trader.portfolio_exists.return_value = False
    resp = _upload(filename="orphan.csv")

    assert resp.status_code == 400
    archived = list((tmp_path / "imported").glob("*_orphan.csv"))
    assert len(archived) == 1


def test_archived_filename_is_sanitized(mocked_import):
    """A crafted filename (e.g. a path-traversal attempt) must not escape
    IMPORTED_FILES_DIR or otherwise produce an unsafe path on disk."""
    _, _, _, tmp_path = mocked_import
    resp = _upload(filename="../../etc/passwd.csv")

    assert resp.status_code == 200
    archived = list((tmp_path / "imported").iterdir())
    assert len(archived) == 1
    # Written directly inside IMPORTED_FILES_DIR, not some traversed-to path.
    assert archived[0].parent == tmp_path / "imported"
    assert "/" not in archived[0].name


def test_happy_path_records_one_notification_event(mocked_import):
    """Each successful import records one notification-centre Event (#184)."""
    _, _, mock_notifications, _ = mocked_import
    resp = _upload(filename="q1_2024.csv")

    assert resp.status_code == 200
    mock_notifications.record.assert_called_once()
    args, kwargs = mock_notifications.record.call_args
    assert args[0] == NotificationCategory.PORTFOLIO
    assert args[1] == "sipp_import"
    assert "q1_2024.csv" in args[2]
    assert "2 buy(s)" in kwargs["body"]
    assert kwargs["severity"] == NotificationSeverity.INFO


def test_notification_includes_note_and_warning_severity_when_rows_skipped(
    mocked_import,
):
    """The skipped-row/unparseable-value note shown in the UI banner must
    also reach the notification-centre event, with the actual per-row
    reason (which row, which column) spelled out rather than just a count,
    and WARNING severity to flag the partial import (#184, #185)."""
    mock_trader, _, mock_notifications, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0,
        buy_count=2,
        sell_count=0,
        cash_flow_count=1,
        skipped_rows=["row 4: non-positive shares/price (0, 100.0)"],
        parse_errors=["row 7: unparseable Price '£abc'"],
    )
    resp = _upload()

    assert resp.status_code == 200
    args, kwargs = mock_notifications.record.call_args
    body = kwargs["body"]
    assert "1 row(s) skipped — row 4: non-positive shares/price (0, 100.0)" in body
    assert "1 value(s) unparseable — row 7: unparseable Price '£abc'" in body
    assert kwargs["severity"] == NotificationSeverity.WARNING


def test_notification_caps_issue_detail_and_notes_the_remainder(mocked_import):
    """A badly malformed file shouldn't dump dozens of row reasons into one
    message — cap the detail and say how many more there were (#185)."""
    mock_trader, _, mock_notifications, _ = mocked_import
    skipped = [f"row {i}: non-positive shares/price (0, 100.0)" for i in range(8)]
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0, buy_count=1, sell_count=0, skipped_rows=skipped
    )
    resp = _upload()

    assert resp.status_code == 200
    body = mock_notifications.record.call_args.kwargs["body"]
    assert "8 row(s) skipped" in body
    assert "(+3 more)" in body


def test_non_csv_is_rejected_without_import(mocked_import):
    mock_trader, mock_portfolio, mock_notifications, _ = mocked_import
    resp = _upload(filename="portfolio.txt")

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_not_called()
    mock_portfolio.default_portfolio_context.assert_called_once()
    mock_notifications.record.assert_not_called()


def test_import_failure_returns_400(mocked_import):
    mock_trader, _, mock_notifications, _ = mocked_import
    mock_trader.import_sipp.side_effect = ValueError("bad row")
    resp = _upload()

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_called_once()
    mock_notifications.record.assert_not_called()


def test_rejected_plan_returns_400_and_never_reads_the_portfolio(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """Story 1.2, AC #2/#4: when ``trader.import_sipp`` reports
    ``status="rejected"`` (at least one row failed, nothing persisted), the
    route must return HTTP 400, never call ``trader.get_portfolio`` (the
    plan changed nothing), surface the failed-row detail in the rendered
    message, and still record an ERROR-severity notification so a rejected
    upload is visible in the notification centre, not silent. Mirrors
    ``test_missing_columns_returns_400``/
    ``test_row_count_mismatch_flags_error_status_and_severity``."""
    mock_trader, _, mock_notifications, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=0.0,
        buy_count=0,
        sell_count=0,
        cash_flow_count=0,
        failed_rows=["row REF-BAD: unparseable quantity 'notanumber'"],
        total_rows=1,
        status="rejected",
    )
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload()

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_called_once()
    mock_trader.get_portfolio.assert_not_called()
    assert "row REF-BAD" in calls[-1]["error_message"]
    assert "unparseable quantity" in calls[-1]["error_message"]
    mock_notifications.record.assert_called_once()
    _, kwargs = mock_notifications.record.call_args
    assert kwargs["severity"] == NotificationSeverity.ERROR
    assert "REF-BAD" in kwargs["body"]


def test_currency_rejected_plan_returns_400_with_issue_detail_rendered(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """Story 1.4, AC3: a currency validation failure (ambiguous/unsupported/
    malformed-locale/contradictory) is a ``failed`` outcome in the same
    FR-13/AD-25 vocabulary Story 1.2 already wires through this route -- no
    new route logic is needed, only that a currency-shaped ``failed_rows``
    entry renders via the existing ``_describe_issues`` helper exactly like
    any other failure reason."""
    mock_trader, _, mock_notifications, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=0.0,
        buy_count=0,
        sell_count=0,
        cash_flow_count=0,
        failed_rows=[
            "row REF-FX: Debit unsupported_currency: unsupported currency in '¥123'"
        ],
        total_rows=1,
        status="rejected",
    )
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload()

    assert resp.status_code == 400
    mock_trader.get_portfolio.assert_not_called()
    assert "REF-FX" in calls[-1]["error_message"]
    assert "unsupported_currency" in calls[-1]["error_message"]
    mock_notifications.record.assert_called_once()


def test_missing_columns_returns_400(mocked_import):
    from app.agents.trader.trader_agent import SippImportError

    mock_trader, _, mock_notifications, _ = mocked_import
    mock_trader.import_sipp.side_effect = SippImportError(
        "CSV is missing required columns: Quantity"
    )
    resp = _upload()

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_called_once()
    mock_notifications.record.assert_not_called()


def test_happy_path_context_carries_buy_sell_counts_for_queue_aggregation(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """The multi-file import queue (index.html's handleSippImportSubmit)
    sums these across sequential single-file POSTs — regression guard for
    #183."""
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload()

    assert resp.status_code == 200
    assert calls[-1]["import_buy_count"] == 2
    assert calls[-1]["import_sell_count"] == 0
    assert calls[-1]["import_cash_count"] == 1
    assert calls[-1]["import_skipped_count"] == 0
    assert calls[-1]["import_status"] == "ok"


def test_duplicate_rows_are_reported_as_duplicates_not_successes(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """Story 1.8, AC #2/#4: re-importing an overlapping CSV must read as
    "already imported", not as a fresh success — the counts the queue
    aggregates carry the duplicate/inserted split, and the prose says so."""
    mock_trader, _, _, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0,
        buy_count=0,
        sell_count=0,
        cash_flow_count=0,
        inserted_count=0,
        duplicate_count=3,
        skipped_count=1,
        total_rows=4,
        status="ok",
    )
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload()

    assert resp.status_code == 200
    assert calls[-1]["import_status"] == "ok"
    assert calls[-1]["import_duplicate_count"] == 3
    assert calls[-1]["import_inserted_count"] == 0
    assert calls[-1]["import_skipped_count"] == 1
    assert calls[-1]["import_failed_count"] == 0
    assert "3 duplicate(s) already imported" in calls[-1]["import_message"]
    assert "1 row(s) skipped" in calls[-1]["import_message"]
    # A duplicate never inflates the buy count.
    assert "Imported 0 buy(s)" in calls[-1]["import_message"]


def test_rejected_plan_reports_all_four_outcomes_as_provisional(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """Story 1.8, AC #5: a rejected plan still reports all four outcomes,
    framed as what *would* have happened so the counts can't be mistaken for
    a partial success."""
    mock_trader, _, _, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=0.0,
        inserted_count=2,
        duplicate_count=1,
        skipped_count=1,
        failed_rows=["row REF-BAD: unparseable quantity 'notanumber'"],
        total_rows=5,
        status="rejected",
    )
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload()

    assert resp.status_code == 400
    message = calls[-1]["error_message"]
    assert "nothing was saved" in message
    assert "2 row(s) would have inserted" in message
    assert "1 would have been duplicates" in message
    assert "1 skipped" in message
    assert "Imported" not in message  # never the ordinary success phrasing
    assert calls[-1]["import_failed_count"] == 1
    assert calls[-1]["import_status"] == "rejected"


def test_row_count_mismatch_flags_error_status_and_severity(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """When TraderAgent.import_sipp reports status="error" (the four outcome
    counts didn't add up to total_rows, #187), the route must carry that
    through: a non-2xx HTTP status (#210, Story 1.3's AC1 — the
    message/severity contract asserted below is #187's, unchanged by that
    story), the banner status context, an ERROR line in the message, and
    ERROR notification severity — not folded into an ordinary WARNING the
    way a data-quality skip is."""
    mock_trader, _, mock_notifications, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0,
        buy_count=2,
        sell_count=0,
        cash_flow_count=1,
        inserted_count=3,
        total_rows=4,
        status="error",
    )
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload()

    assert resp.status_code == 400
    assert calls[-1]["import_status"] == "error"
    assert "ERROR" in calls[-1]["import_message"]
    assert "3 of 4" in calls[-1]["import_message"]
    assert mock_notifications.record.call_args.kwargs["severity"] == (
        NotificationSeverity.ERROR
    )


def test_row_count_mismatch_response_is_not_a_success_status(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """Dedicated, single-purpose regression proof of AC1: a status="error"
    result never returns a 2xx status, independent of the message/severity
    assertions in the test above."""
    mock_trader, _, _, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0,
        buy_count=2,
        sell_count=0,
        cash_flow_count=1,
        total_rows=4,
        status="error",
    )
    resp = _upload()

    assert resp.status_code >= 400
    assert resp.status_code < 500


_STALE_HEADER = (
    "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
    "Running Balance\n"
)
_NEWER_CSV = (
    _STALE_HEADER + "01/06/2024,MSFT,B1,5,200,Buy MSFT,N1,1000,,9000\n"
).encode()
_OLDER_CSV = (
    _STALE_HEADER + "01/01/2024,AAPL,B0,10,100,Buy AAPL,O1,1000,,4000\n"
).encode()


def test_rejected_stale_balance_never_reaches_the_rendered_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Story 1.9 AC1, route level, against a real agent rather than a mock.

    The bug lives inside ``TraderAgent.import_sipp``'s own value flow, so a
    ``MagicMock`` import_sipp cannot reproduce it — the route faithfully
    forwards whatever the result carries. Import a newer file, then an older
    one whose balance the stale-date guard rejects, and assert the second
    render shows the still-in-effect 9000, never the rejected 4000.
    """
    from app.agents.trader.trader_agent import TraderAgent
    from app.services.portfolio_service import PortfolioService
    from app.services.trader_service import TraderService

    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.portfolio.IMPORTED_FILES_DIR", tmp_path / "imported"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    trader = TraderService(agent)

    contexts = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            contexts.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: PortfolioService(trader)
    app.dependency_overrides[get_notifications_repository] = lambda: MagicMock()
    try:
        _upload(content=_NEWER_CSV, portfolio_id=pf.id)
        _upload(content=_OLDER_CSV, portfolio_id=pf.id)
    finally:
        app.dependency_overrides.clear()

    assert contexts[-1]["cash_balance"] == 9000.0
    assert "9,000.00" in contexts[-1]["import_message"]
    assert "4,000.00" not in contexts[-1]["import_message"]


_NO_BALANCE_CSV = (
    _STALE_HEADER + "01/01/2024,AAPL,B0,10,100,Buy AAPL,O1,1000,,\n"
).encode()


def test_import_with_no_cash_balance_row_never_shows_a_phantom_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Story 1.9 AC2, route level, against a real agent rather than a mock.

    ``SippImportResult.cash_balance`` is a non-optional ``float`` — 0.0 both
    for a genuine zero and for "never recorded" — so a route that passed it
    straight through to the template would render a phantom CASH £0.00 row
    for a stock-only import into a portfolio with no cash history at all.
    The route must re-read ``get_cash_balance`` (``None`` when unset) rather
    than trust the result, so the "no data" case keeps hiding the CASH row.
    """
    from app.agents.trader.trader_agent import TraderAgent
    from app.services.portfolio_service import PortfolioService
    from app.services.trader_service import TraderService

    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.portfolio.IMPORTED_FILES_DIR", tmp_path / "imported"
    )
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    trader = TraderService(agent)

    contexts = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            contexts.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: PortfolioService(trader)
    app.dependency_overrides[get_notifications_repository] = lambda: MagicMock()
    try:
        resp = _upload(content=_NO_BALANCE_CSV, portfolio_id=pf.id)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, "import did not reach the success path"
    assert contexts, "TemplateResponse was never called"
    assert agent.get_cash_balance(pf.id) is None
    assert contexts[-1]["cash_balance"] is None
    assert "none on record" in contexts[-1]["import_message"]
    assert "£0.00" not in contexts[-1]["import_message"]
    # Prove the real template render hides the row too, not just the
    # context value -- a context-only assertion can't catch a future
    # regression in the "is not none" gate itself.
    rendered = templates.get_template("_portfolio.html").render(**contexts[-1])
    assert (
        '<span class="fw-bold" style="color:#0f2744; font-size:0.85rem">CASH</span>'
        not in rendered
    )


def test_reconciliation_route_renders_detected_issue(tmp_path: Path) -> None:
    """Story 1.5, AC5: the dedicated reconciliation view route returns 200
    and renders a detected issue's date/expected/actual/difference -- no
    new route-level logic beyond a thin passthrough was needed, since this
    is an unauthenticated GET (read) matching ``/partials/portfolio``."""
    from app.agents.trader.trader_agent import TraderAgent
    from app.services.trader_service import TraderService

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "03/01/2024,n/a,n/a,n/a,n/a,Statement,R3,,,600.00\n"
        "02/01/2024,n/a,n/a,n/a,n/a,Contribution,R2,,100.00,500.00\n"
        "01/01/2024,n/a,n/a,n/a,n/a,Opening,R1,,,400.00\n"
    )
    agent.import_sipp(csv_text.encode("utf-8"), portfolio_id=pf.id)

    trader = TraderService(agent)
    app.dependency_overrides[get_trader_service] = lambda: trader
    try:
        resp = client.get(f"/portfolio/{pf.id}/reconciliation")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "2024-01-03" in resp.text
    assert "500.00" in resp.text
    assert "600.00" in resp.text
    assert "R3" in resp.text


def test_portfolio_partial_renders_non_gbp_balance_without_a_fabricated_gbp_figure(
    tmp_path: Path,
) -> None:
    """Story 1.6, AC1/AC3: a non-GBP cash balance renders with its real
    currency code -- never a bare/£-prefixed number -- and, when the GBP
    valuation is unavailable, an explicit unavailable marker rather than a
    computed figure. Injects a fake ``GbpValuationService`` so the test
    never touches real yfinance."""
    from typing import Any, cast

    from app.agents.trader.trader_agent import TraderAgent
    from app.services.gbp_valuation_service import GbpValuationProjection
    from app.services.portfolio_service import PortfolioService
    from app.services.trader_service import TraderService

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,US dividend,n/a,n/a,$154.86,$154.86\n"
    )
    agent.import_sipp(csv_text.encode("utf-8"), portfolio_id=pf.id)

    trader = TraderService(agent)

    class _FakeValuation:
        def value_in_gbp(self, money: Any) -> GbpValuationProjection:
            return GbpValuationProjection(
                money=money, status="valuation_unavailable", reason="fetch_failed"
            )

    portfolio = PortfolioService(trader, gbp_valuation=cast(Any, _FakeValuation()))
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: portfolio
    try:
        resp = client.get(f"/partials/portfolio?portfolio_id={pf.id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "USD 154.86" in resp.text
    assert "£154.86" not in resp.text
    assert "GBP valuation unavailable" in resp.text


def test_cash_flow_ledger_renders_real_currency_not_a_fabricated_gbp_prefix(
    tmp_path: Path,
) -> None:
    """A ``CashFlow`` row with ``currency="USD"`` renders with its real
    currency, not £; a ``currency="GBP"`` row (today's default, and every
    currency-less legacy row) renders unchanged."""
    from typing import Any, cast

    from app.agents.trader.trader_agent import TraderAgent
    from app.services.gbp_valuation_service import GbpValuationProjection
    from app.services.portfolio_service import PortfolioService
    from app.services.trader_service import TraderService

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,n/a,n/a,n/a,US dividend,USD1,n/a,$1.25,$1.25\n"
        "02/01/2024,n/a,n/a,n/a,n/a,GBP interest,GBP1,n/a,10.00,11.25\n"
    )
    agent.import_sipp(csv_text.encode("utf-8"), portfolio_id=pf.id)

    trader = TraderService(agent)

    class _FakeValuation:
        def value_in_gbp(self, money: Any) -> GbpValuationProjection:
            return GbpValuationProjection(
                money=money, status="valuation_unavailable", reason="fetch_failed"
            )

    portfolio = PortfolioService(trader, gbp_valuation=cast(Any, _FakeValuation()))
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: portfolio
    try:
        resp = client.get(f"/partials/portfolio?portfolio_id={pf.id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "USD 1.25" in resp.text
    assert "£10.00" in resp.text
    assert "£1.25" not in resp.text


def test_forbidden_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post("/import-sipp", files={"file": ("merged.csv", _CSV, "text/csv")})
    assert resp.status_code == 403


def test_route_module_no_longer_imports_sipp_import_dir():
    """AC #2, import-graph level: the retired shared path constant must not
    even be reachable from the route module, not just unused this run
    (#210)."""
    import app.api.routes.portfolio as portfolio_route

    assert not hasattr(portfolio_route, "SIPP_IMPORT_DIR")


def test_rendered_portfolio_partial_includes_portfolio_select_element():
    """Structural regression guard for Gate 2 (AC3/AC4): index.html's
    handleSippImportSubmit keys off `#portfolioSelect` to disable/re-enable
    the account selector for the duration of an in-flight import queue.
    This proves the element the JS depends on is actually present in a
    normal (non-empty) rendered `_portfolio.html` partial, so a future
    markup rename can't silently break Gate 2's lock/discard logic without
    a test failing. This is a structural proof only — it does not execute
    JavaScript or prove the runtime disable/discard behavior itself; see
    the story's Dev Notes "No JS Test Harness Exists" for why, and the new
    Playwright module for the behavioral proof."""
    mock_trader = MagicMock()
    mock_trader.list_portfolios.return_value = [
        Portfolio(id=1, name="SIPP", created_at="2024-01-01")
    ]
    mock_trader.load_price_cache.return_value = ({}, None, {})
    mock_trader.get_portfolio.return_value = []
    mock_trader.get_cash_balance.return_value = 0.0
    mock_trader.get_cash_flows.return_value = []
    mock_trader.snapshot_history.return_value = []

    service = PortfolioService(mock_trader)
    ctx = service.default_portfolio_context(1)
    html = templates.get_template("_portfolio.html").render(**ctx)

    assert 'id="portfolioSelect"' in html
