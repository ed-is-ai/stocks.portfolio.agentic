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
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.trade import SippImportResult

client = TestClient(app)

_CSV = b"Date,Symbol,Quantity,Price,Running Balance\n01/01/2024,AAPL,10,100,5000\n"


@pytest.fixture
def mocked_import(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: HTMLResponse("ok", status_code=k.get("status_code", 200)),
    )
    monkeypatch.setattr("app.api.routes.portfolio.SIPP_IMPORT_DIR", tmp_path / "SIPP")
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


def _upload(filename: str = "merged.csv", content: bytes = _CSV, token: str = "s3cret"):
    headers = {"X-Auth-Token": token} if token else {}
    return client.post(
        "/import-sipp",
        files={"file": (filename, content, "text/csv")},
        data={"portfolio_id": "1"},
        headers=headers,
    )


def test_happy_path_imports_and_saves(mocked_import):
    mock_trader, _, _, tmp_path = mocked_import
    resp = _upload()

    assert resp.status_code == 200
    # import_sipp called once with the saved CSV path
    mock_trader.import_sipp.assert_called_once()
    saved_path = Path(mock_trader.import_sipp.call_args.args[0])
    assert saved_path == tmp_path / "SIPP" / "merged.csv"
    # the uploaded bytes were persisted
    assert saved_path.read_bytes() == _CSV
    mock_trader.get_portfolio.assert_called_once()


def test_happy_path_archives_a_copy_of_the_uploaded_file(mocked_import):
    """A successful import must leave a permanent, timestamped copy under
    data/imported/ — unlike data/processed/SIPP/merged.csv, which the next
    upload overwrites."""
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


def test_row_count_mismatch_flags_error_status_and_severity(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """When TraderAgent.import_sipp reports status="error" (buy/sell/cash/
    skipped counts didn't add up to total_rows, #187), the route must carry
    that through: the banner status context, an ERROR line in the message,
    and ERROR notification severity — not folded into an ordinary WARNING
    the way a data-quality skip is."""
    mock_trader, _, mock_notifications, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0,
        buy_count=2,
        sell_count=0,
        cash_flow_count=1,
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

    assert resp.status_code == 200
    assert calls[-1]["import_status"] == "error"
    assert "ERROR" in calls[-1]["import_message"]
    assert "3 of 4" in calls[-1]["import_message"]
    assert mock_notifications.record.call_args.kwargs["severity"] == (
        NotificationSeverity.ERROR
    )


def test_forbidden_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post("/import-sipp", files={"file": ("merged.csv", _CSV, "text/csv")})
    assert resp.status_code == 403
