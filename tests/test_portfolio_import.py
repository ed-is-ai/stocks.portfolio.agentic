"""Tests for the POST /import-sipp portfolio CSV upload route (#145)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
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
from app.core.config import IMPORTED_FILES_RETENTION_DAYS, imported_file_max_bytes
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
    provider_id: str | None = "interactive_investor",
    account_type_id: str | None = "sipp",
):
    headers = {"X-Auth-Token": token} if token else {}
    data = {"portfolio_id": str(portfolio_id)}
    if provider_id is not None:
        data["provider_id"] = provider_id
    if account_type_id is not None:
        data["account_type_id"] = account_type_id
    return client.post(
        "/import-sipp",
        files={"file": (filename, content, "text/csv")},
        data=data,
        headers=headers,
    )


def test_happy_path_passes_uploaded_bytes_directly(mocked_import):
    mock_trader, mock_portfolio, _, tmp_path = mocked_import
    resp = _upload()

    assert resp.status_code == 200
    # import_sipp is called once with the raw uploaded bytes, not a saved
    # path — no shared filesystem location is written or read (#210).
    mock_trader.import_sipp.assert_called_once()
    assert mock_trader.import_sipp.call_args.args[0] == _CSV
    assert not (tmp_path / "SIPP").exists()
    mock_portfolio.portfolio_input_snapshot.assert_called_once_with(
        1, cash_balance=5000.0
    )
    mock_portfolio.positions_from_input_snapshot.assert_called_once_with(
        mock_portfolio.portfolio_input_snapshot.return_value
    )


def test_refresh_uses_one_public_input_snapshot_for_positions_and_context(
    mocked_import,
):
    mock_trader, mock_portfolio, _, _ = mocked_import
    snapshot = MagicMock(cash_balance=5000.0)
    chart_snapshot = MagicMock(cash_balance=5000.0)
    initial_positions = [MagicMock(ticker="AAPL")]
    refreshed_positions = [MagicMock(ticker="AAPL")]
    mock_portfolio.portfolio_input_snapshot.return_value = snapshot
    mock_portfolio.with_current_chart_data.return_value = chart_snapshot
    mock_portfolio.positions_from_input_snapshot.side_effect = [
        initial_positions,
        refreshed_positions,
    ]
    mock_portfolio.gbpusd_rate.return_value = 1.25
    mock_portfolio.load_ticker_aliases.return_value = {}
    mock_portfolio.fetch_all_prices_with_failures.return_value = (
        {"AAPL": 100.0},
        {},
        set(),
    )
    mock_trader.load_price_cache.return_value = ({"AAPL": 100.0}, "now", {})

    response = client.post("/api/portfolio/refresh", headers={"X-Auth-Token": "s3cret"})

    assert response.status_code == 200
    mock_portfolio.portfolio_input_snapshot.assert_called_once_with(None)
    assert mock_portfolio.positions_from_input_snapshot.call_args_list == [
        ((snapshot,), {}),
        ((snapshot, {"AAPL": 100.0}, {}), {}),
    ]
    mock_portfolio.portfolio_partial_context.assert_called_once_with(
        refreshed_positions,
        prices_as_of="now",
        gbpusd_rate=1.25,
        cash_balance=5000.0,
        warning_message=None,
        portfolio_id=None,
        input_snapshot=chart_snapshot,
    )
    mock_portfolio.with_current_chart_data.assert_called_once_with(snapshot, None)
    mock_trader.get_portfolio.assert_not_called()
    mock_trader.refresh_portfolio_prices.assert_not_called()


def test_refresh_keeps_cached_failures_and_names_unavailable_symbols(mocked_import):
    mock_trader, mock_portfolio, _, _ = mocked_import
    positions = [
        SimpleNamespace(ticker="CACHED"),
        SimpleNamespace(ticker="MISSING"),
        SimpleNamespace(ticker="FRESH"),
    ]
    snapshot = MagicMock(cash_balance=0.0)
    mock_portfolio.portfolio_input_snapshot.return_value = snapshot
    mock_portfolio.positions_from_input_snapshot.side_effect = [positions, positions]
    mock_trader.load_price_cache.return_value = (
        {"CACHED": 10.0},
        "now",
        {"CACHED": (10.0, "GBP")},
    )
    mock_trader.get_cash_balance.return_value = 0.0
    mock_portfolio.gbpusd_rate.return_value = 1.25
    mock_portfolio.load_ticker_aliases.return_value = {}
    mock_portfolio.fetch_all_prices_with_failures.return_value = (
        {"FRESH": 20.0},
        {"FRESH": (20.0, "GBP")},
        {"CACHED", "MISSING"},
    )

    response = client.post("/api/portfolio/refresh", headers={"X-Auth-Token": "s3cret"})

    assert response.status_code == 200
    assert mock_portfolio.positions_from_input_snapshot.call_args_list == [
        ((snapshot,), {}),
        (
            (
                snapshot,
                {"CACHED": 10.0, "FRESH": 20.0},
                {"CACHED": (10.0, "GBP"), "FRESH": (20.0, "GBP")},
            ),
            {},
        ),
    ]
    mock_trader.refresh_portfolio_prices.assert_not_called()
    assert (
        "using cached values for: CACHED"
        in (
            mock_portfolio.portfolio_partial_context.call_args.kwargs["warning_message"]
        )
    )
    assert (
        "prices unavailable for: MISSING"
        in (
            mock_portfolio.portfolio_partial_context.call_args.kwargs["warning_message"]
        )
    )


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


def test_failed_import_is_never_archived(mocked_import):
    """A file that fails to import never reached a committed result, so it
    must never be archived (AC2)."""
    mock_trader, _, _, tmp_path = mocked_import
    mock_trader.import_sipp.side_effect = ValueError("bad row")
    resp = _upload(filename="bad.csv")

    assert resp.status_code == 400
    assert not (tmp_path / "imported").exists()


def test_rejected_non_csv_upload_is_never_archived(mocked_import):
    """A wrong-extension upload — rejected before any import is attempted —
    is never archived (AC2)."""
    _, _, _, tmp_path = mocked_import
    resp = _upload(filename="portfolio.txt")

    assert resp.status_code == 400
    assert not (tmp_path / "imported").exists()


def test_missing_portfolio_upload_is_never_archived(mocked_import):
    """The earliest rejection point — no valid portfolio selected, before
    the file is even parsed — is never archived (AC2)."""
    mock_trader, _, _, tmp_path = mocked_import
    mock_trader.portfolio_exists.return_value = False
    resp = _upload(filename="orphan.csv")

    assert resp.status_code == 400
    assert not (tmp_path / "imported").exists()


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


def test_rejected_import_plan_is_never_archived(mocked_import):
    """Story 1.2's whole-plan-rejection case (AC2): a plan with at least one
    failed row commits nothing, so the source file must never be archived."""
    mock_trader, _, _, tmp_path = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        status="rejected", cash_balance=0.0
    )
    resp = _upload(filename="rejected.csv")

    assert resp.status_code == 400
    assert not (tmp_path / "imported").exists()


def test_error_status_import_is_still_archived(mocked_import):
    """``status="error"`` (#187's row-accounting-mismatch safety net) still
    means the plan's writes were committed, so it must still be archived —
    the file helps diagnose why the count didn't add up (AC1)."""
    mock_trader, _, _, tmp_path = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        status="error", cash_balance=5000.0
    )
    resp = _upload(filename="mismatch.csv")

    assert resp.status_code == 400
    archived = list((tmp_path / "imported").glob("*_mismatch.csv"))
    assert len(archived) == 1


def test_oversized_upload_is_not_archived(mocked_import, monkeypatch):
    """A committed import whose file exceeds the configured max size is not
    archived (AC4), but the import result itself is unaffected (AC5)."""
    _, _, _, tmp_path = mocked_import
    monkeypatch.setattr(
        "app.api.routes.portfolio.imported_file_max_bytes", lambda: len(_CSV) - 1
    )
    resp = _upload(filename="big.csv")

    assert resp.status_code == 200
    assert not (tmp_path / "imported").exists()


def test_oversized_upload_still_prunes_old_archives(mocked_import, monkeypatch):
    """Pruning runs on every archive *attempt*, not just a successful write,
    so a stretch of only-oversized uploads doesn't starve retention (AC3
    review finding: prune-on-every-attempt decision, 2026-08-13)."""
    _, _, _, tmp_path = mocked_import
    archive_dir = tmp_path / "imported"
    archive_dir.mkdir(parents=True)
    old_file = archive_dir / "old_file.csv"
    old_file.write_bytes(b"stale")
    old_ts = time.time() - 8 * 86400
    os.utime(old_file, (old_ts, old_ts))
    monkeypatch.setattr(
        "app.api.routes.portfolio.imported_file_max_bytes", lambda: len(_CSV) - 1
    )

    resp = _upload(filename="big.csv")

    assert resp.status_code == 200
    assert not old_file.exists()
    assert list(archive_dir.iterdir()) == []


def test_upload_at_exactly_max_bytes_is_archived(mocked_import, monkeypatch):
    """The size check is a strict ``>``, not ``>=`` — a file exactly at the
    configured limit is still archived (AC4's boundary)."""
    _, _, _, tmp_path = mocked_import
    monkeypatch.setattr(
        "app.api.routes.portfolio.imported_file_max_bytes", lambda: len(_CSV)
    )
    resp = _upload(filename="exact.csv")

    assert resp.status_code == 200
    archived = list((tmp_path / "imported").glob("*_exact.csv"))
    assert len(archived) == 1


def test_archive_retention_prunes_files_older_than_seven_days(mocked_import):
    """An archived file older than the retention window is deleted the next
    time a successful import runs (AC3)."""
    _, _, _, tmp_path = mocked_import
    archive_dir = tmp_path / "imported"
    archive_dir.mkdir(parents=True)
    old_file = archive_dir / "old_file.csv"
    old_file.write_bytes(b"stale")
    old_ts = time.time() - 8 * 86400
    os.utime(old_file, (old_ts, old_ts))

    resp = _upload(filename="fresh.csv")

    assert resp.status_code == 200
    remaining = list(archive_dir.iterdir())
    assert old_file not in remaining
    assert len(remaining) == 1


def test_archive_retention_keeps_recent_files(mocked_import):
    """An archived file within the retention window survives a prune run
    triggered by a later successful import (AC3's 7-day boundary)."""
    _, _, _, tmp_path = mocked_import
    archive_dir = tmp_path / "imported"
    archive_dir.mkdir(parents=True)
    recent_file = archive_dir / "recent_file.csv"
    recent_file.write_bytes(b"recent")
    recent_ts = time.time() - 3 * 86400
    os.utime(recent_file, (recent_ts, recent_ts))

    resp = _upload(filename="fresh.csv")

    assert resp.status_code == 200
    assert recent_file.exists()


def test_archive_retention_boundary_just_under_cutoff_is_kept(mocked_import):
    """A file just inside the retention window (based on the actual
    ``IMPORTED_FILES_RETENTION_DAYS`` constant, not a hardcoded day count)
    survives a prune run — proves the cutoff math itself, not just an
    8-day-vs-3-day example."""
    _, _, _, tmp_path = mocked_import
    archive_dir = tmp_path / "imported"
    archive_dir.mkdir(parents=True)
    boundary_file = archive_dir / "boundary_file.csv"
    boundary_file.write_bytes(b"boundary")
    boundary_ts = time.time() - (IMPORTED_FILES_RETENTION_DAYS * 86400 - 60)
    os.utime(boundary_file, (boundary_ts, boundary_ts))

    resp = _upload(filename="fresh.csv")

    assert resp.status_code == 200
    assert boundary_file.exists()


def test_archive_retention_boundary_just_over_cutoff_is_pruned(mocked_import):
    """A file just past the retention window (based on the actual
    ``IMPORTED_FILES_RETENTION_DAYS`` constant) is pruned — the other side
    of the same boundary as the "just under" test above."""
    _, _, _, tmp_path = mocked_import
    archive_dir = tmp_path / "imported"
    archive_dir.mkdir(parents=True)
    boundary_file = archive_dir / "boundary_file.csv"
    boundary_file.write_bytes(b"boundary")
    boundary_ts = time.time() - (IMPORTED_FILES_RETENTION_DAYS * 86400 + 60)
    os.utime(boundary_file, (boundary_ts, boundary_ts))

    resp = _upload(filename="fresh.csv")

    assert resp.status_code == 200
    assert not boundary_file.exists()


def test_archive_write_failure_does_not_fail_the_import(mocked_import, monkeypatch):
    """A failure inside the archive write step must only be logged and must
    never change whether the import itself is reported succeeded or failed
    (AC5) — this must hold now that archiving runs after ``result`` is known
    (and before the response is built), not merely because archiving used
    to run before ``import_sipp`` was even called."""
    _, _, _, tmp_path = mocked_import

    def _boom(self, content):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_bytes", _boom)
    resp = _upload(filename="q1_2024.csv")

    assert resp.status_code == 200
    archive_dir = tmp_path / "imported"
    assert not archive_dir.exists() or not list(archive_dir.iterdir())


def test_imported_file_max_bytes_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("IMPORTED_FILE_MAX_BYTES", raising=False)
    default = imported_file_max_bytes()

    monkeypatch.setenv("IMPORTED_FILE_MAX_BYTES", "not-a-number")
    assert imported_file_max_bytes() == default
    monkeypatch.setenv("IMPORTED_FILE_MAX_BYTES", "0")
    assert imported_file_max_bytes() == default
    monkeypatch.setenv("IMPORTED_FILE_MAX_BYTES", "-5")
    assert imported_file_max_bytes() == default


def test_imported_file_max_bytes_uses_valid_env_value(monkeypatch):
    monkeypatch.setenv("IMPORTED_FILE_MAX_BYTES", "12345")
    assert imported_file_max_bytes() == 12345


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


# --- Story 3.3: provider/account-type selection ------------------------


def test_missing_provider_id_returns_400_without_calling_import(mocked_import):
    """AC3: an omitted provider selection is rejected before the agent is
    ever called -- no write, no archive."""
    mock_trader, _, mock_notifications, tmp_path = mocked_import
    resp = _upload(provider_id=None)

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_not_called()
    mock_notifications.record.assert_not_called()
    assert not (tmp_path / "imported").exists()


def test_unknown_provider_id_returns_400_without_calling_import(mocked_import):
    mock_trader, _, mock_notifications, tmp_path = mocked_import
    resp = _upload(provider_id="fake_broker")

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_not_called()
    mock_notifications.record.assert_not_called()
    assert not (tmp_path / "imported").exists()


def test_provider_id_is_forwarded_to_trader_import_sipp(mocked_import):
    """The route's own validation only checks known providers -- the
    detected-contract mismatch check happens inside ``TraderAgent.
    import_sipp``, so the route must forward the selection through."""
    mock_trader, _, _, _ = mocked_import
    _upload(provider_id="interactive_investor", account_type_id="sipp")

    mock_trader.import_sipp.assert_called_once_with(
        _CSV, 1, "interactive_investor", "sipp"
    )


def test_provider_contract_mismatch_returns_400_without_write(mocked_import):
    """A provider/account-type mismatch surfaces as ``SippImportError`` from
    the agent -- the route must not write or archive anything (I/O matrix:
    "Tampered/mismatched provider")."""
    from app.agents.trader.trader_agent import SippImportError

    mock_trader, _, mock_notifications, tmp_path = mocked_import
    mock_trader.import_sipp.side_effect = SippImportError(
        "selected provider does not match this CSV's detected provider"
    )
    resp = _upload(provider_id="interactive_investor", account_type_id="sipp")

    assert resp.status_code == 400
    mock_notifications.record.assert_not_called()
    assert not (tmp_path / "imported").exists()


def test_account_type_omitted_returns_400_without_write(mocked_import):
    """An omitted account-type against a contract that declares types is
    rejected inside the agent -- the route surfaces it as a plain 400, no
    write."""
    from app.agents.trader.trader_agent import SippImportError

    mock_trader, _, mock_notifications, tmp_path = mocked_import
    mock_trader.import_sipp.side_effect = SippImportError(
        "select an account type: sipp"
    )
    resp = _upload(provider_id="interactive_investor", account_type_id=None)

    assert resp.status_code == 400
    mock_notifications.record.assert_not_called()
    assert not (tmp_path / "imported").exists()


def test_successful_import_echoes_provider_account_type_and_version(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """AC5: a successful import's context carries the server-validated
    provider name/account type/contract version, never the raw form
    field."""
    mock_trader, _, _, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0,
        buy_count=2,
        sell_count=0,
        cash_flow_count=1,
        provider_id="interactive_investor",
        provider_name="Interactive Investor",
        contract_version="1",
        account_type_id="sipp",
    )
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload(provider_id="interactive_investor", account_type_id="sipp")

    assert resp.status_code == 200
    assert calls[-1]["import_provider_name"] == "Interactive Investor"
    assert calls[-1]["import_account_type"] == "sipp"
    assert calls[-1]["import_contract_version"] == "1"


def test_rejected_plan_context_carries_bounded_failed_rows_and_preview(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """AC4: a rejected plan's context carries every discoverable row issue
    (bounded, with an omitted count beyond the bound) plus the redacted
    preview rows, every row sharing identical keys."""
    failed_rows = [f"row {i}: bad" for i in range(210)]
    preview_rows = [
        {"date": "01/01/2024", "security_symbol": "AAPL", "quantity": "10"},
        {"date": "02/01/2024", "security_symbol": "", "quantity": ""},
    ]
    mock_trader, _, _, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=0.0,
        failed_rows=failed_rows,
        total_rows=210,
        status="rejected",
        preview_rows=preview_rows,
    )
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload(provider_id="interactive_investor", account_type_id="sipp")

    assert resp.status_code == 400
    ctx = calls[-1]
    assert len(ctx["import_failed_rows"]) == 200
    assert ctx["import_failed_rows_omitted"] == 10
    assert ctx["import_preview_rows"] == preview_rows
    keys = {frozenset(row.keys()) for row in ctx["import_preview_rows"]}
    assert len(keys) == 1


def test_generic_exception_path_does_not_leak_exception_text(
    monkeypatch: pytest.MonkeyPatch, mocked_import
):
    """AC4/Boundaries: an unexpected exception must never echo
    ``str(exception)``, a file path, or contract source internals -- only
    the fixed, generic message."""
    mock_trader, _, mock_notifications, _ = mocked_import
    mock_trader.import_sipp.side_effect = RuntimeError(
        "secret database path: /var/data/trades.db leaked"
    )
    calls = []
    monkeypatch.setattr(
        "app.api.routes.portfolio.templates.TemplateResponse",
        lambda *a, **k: (
            calls.append(k.get("context", {}))
            or HTMLResponse("ok", status_code=k.get("status_code", 200))
        ),
    )
    resp = _upload(provider_id="interactive_investor", account_type_id="sipp")

    assert resp.status_code == 400
    assert calls[-1]["error_message"] == "Import failed — see server logs."
    assert "/var/data/trades.db" not in calls[-1]["error_message"]
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
    agent.import_sipp(
        csv_text.encode("utf-8"), portfolio_id=pf.id, account_type_id="sipp"
    )

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
    agent.import_sipp(
        csv_text.encode("utf-8"), portfolio_id=pf.id, account_type_id="sipp"
    )

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
    agent.import_sipp(
        csv_text.encode("utf-8"), portfolio_id=pf.id, account_type_id="sipp"
    )

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


# --- Story 3.4: IG, a contract-only provider --------------------------------

_IG_FIXTURE_CSV = (
    Path(__file__).parent / "fixtures" / "ig" / "valid_mixed.csv"
).read_bytes()

#: A minimal but *complete* II-shaped CSV (unlike the module-level ``_CSV``,
#: which omits Description/Reference/Debit/Credit and is only ever fed to a
#: mocked ``import_sipp`` elsewhere in this file) -- needed here because this
#: test exercises a real, non-mocked ``TraderAgent``/contract-detection path.
_II_FIXTURE_CSV = (
    b"Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
    b"Running Balance\n"
    b"01/01/2024,AAPL,B123,10,100.00,Buy AAPL,REF-1,1000.00,,5000.00\n"
)


def test_ig_route_level_import_succeeds_with_provider_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Route-level proof (AC1/AC3): selecting ``provider_id="ig"`` and
    ``account_type_id="share_dealing"``, uploading a fabricated IG-shaped
    CSV, succeeds through the real (non-mocked) ``TraderAgent``/
    ``TraderService`` stack and echoes provider name "IG"/contract version
    "1" with correct outcome counts -- proving no route-level behavior is
    provider-specific."""
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
    pf = agent.create_portfolio("IG Route Test")
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
        resp = _upload(
            content=_IG_FIXTURE_CSV,
            portfolio_id=pf.id,
            provider_id="ig",
            account_type_id="share_dealing",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert contexts[-1]["import_provider_name"] == "IG"
    assert contexts[-1]["import_contract_version"] == "1"
    assert contexts[-1]["import_account_type"] == "share_dealing"
    assert agent.get_cash_balance(pf.id) == pytest.approx(689.50)


@pytest.mark.parametrize(
    ("provider_id", "account_type_id", "csv_bytes"),
    [
        pytest.param(
            "interactive_investor", "sipp", _II_FIXTURE_CSV, id="interactive_investor"
        ),
        pytest.param("ig", "share_dealing", _IG_FIXTURE_CSV, id="ig"),
    ],
)
def test_route_conformance_succeeds_for_every_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_id: str,
    account_type_id: str,
    csv_bytes: bytes,
) -> None:
    """Parameterized route conformance across both II and IG: the same
    upload flow, against the same real agent/service stack, succeeds
    identically in kind for either provider -- no route-level behavior is
    provider-specific (AC4)."""
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
    pf = agent.create_portfolio("Conformance Test")
    trader = TraderService(agent)

    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: PortfolioService(trader)
    app.dependency_overrides[get_notifications_repository] = lambda: MagicMock()
    try:
        resp = _upload(
            content=csv_bytes,
            portfolio_id=pf.id,
            provider_id=provider_id,
            account_type_id=account_type_id,
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200


# --- #502: snapshot backfill scheduled off the request path --------------


def test_import_success_schedules_snapshot_backfill(mocked_import):
    mock_trader, _, _, _ = mocked_import

    resp = _upload()

    assert resp.status_code == 200
    mock_trader.backfill_snapshots.assert_called_once_with(1)


def test_import_rejected_plan_does_not_schedule_backfill(mocked_import):
    mock_trader, mock_portfolio, _, _ = mocked_import
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=0.0, status="rejected", failed_rows=["row 1: bad"]
    )
    mock_portfolio.default_portfolio_context.return_value = {}

    resp = _upload()

    assert resp.status_code == 400
    mock_trader.backfill_snapshots.assert_not_called()


def test_import_backfill_exception_does_not_change_status(mocked_import):
    mock_trader, _, _, _ = mocked_import
    mock_trader.backfill_snapshots.side_effect = RuntimeError("boom")

    resp = _upload()

    assert resp.status_code == 200
    mock_trader.backfill_snapshots.assert_called_once_with(1)


def test_refresh_success_schedules_backfill_for_that_portfolio(mocked_import):
    mock_trader, mock_portfolio, _, _ = mocked_import
    snapshot = MagicMock(cash_balance=0.0)
    positions = [SimpleNamespace(ticker="AAPL")]
    mock_portfolio.portfolio_input_snapshot.return_value = snapshot
    mock_portfolio.positions_from_input_snapshot.side_effect = [positions, positions]
    mock_portfolio.gbpusd_rate.return_value = 1.25
    mock_portfolio.load_ticker_aliases.return_value = {}
    mock_portfolio.fetch_all_prices_with_failures.return_value = (
        {"AAPL": 1.0},
        {},
        set(),
    )
    mock_trader.load_price_cache.return_value = ({"AAPL": 1.0}, "now", {})
    mock_trader.backfill_snapshots.side_effect = RuntimeError("boom")

    resp = client.post(
        "/api/portfolio/refresh?portfolio_id=7", headers={"X-Auth-Token": "s3cret"}
    )

    assert resp.status_code == 200
    mock_trader.backfill_snapshots.assert_called_once_with(7)


def test_refresh_without_portfolio_id_does_not_schedule_backfill(mocked_import):
    mock_trader, mock_portfolio, _, _ = mocked_import
    snapshot = MagicMock(cash_balance=0.0)
    positions = [SimpleNamespace(ticker="AAPL")]
    mock_portfolio.portfolio_input_snapshot.return_value = snapshot
    mock_portfolio.positions_from_input_snapshot.side_effect = [positions, positions]
    mock_portfolio.gbpusd_rate.return_value = 1.25
    mock_portfolio.load_ticker_aliases.return_value = {}
    mock_portfolio.fetch_all_prices_with_failures.return_value = (
        {"AAPL": 1.0},
        {},
        set(),
    )
    mock_trader.load_price_cache.return_value = ({"AAPL": 1.0}, "now", {})

    resp = client.post("/api/portfolio/refresh", headers={"X-Auth-Token": "s3cret"})

    assert resp.status_code == 200
    mock_trader.backfill_snapshots.assert_not_called()
