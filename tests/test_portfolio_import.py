"""Tests for the POST /import-sipp portfolio CSV upload route (#145)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_portfolio_service, get_trader_service
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
    mock_trader = MagicMock()
    mock_trader.portfolio_exists.return_value = True
    mock_trader.import_sipp.return_value = SippImportResult(
        cash_balance=5000.0, buy_count=2, sell_count=0, cash_flow_count=1
    )
    mock_trader.get_portfolio.return_value = [object(), object()]
    mock_portfolio = MagicMock()
    mock_portfolio.default_portfolio_context.return_value = {}
    mock_portfolio.portfolio_partial_context.return_value = {}
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    try:
        yield mock_trader, mock_portfolio, tmp_path
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
    mock_trader, _, tmp_path = mocked_import
    resp = _upload()

    assert resp.status_code == 200
    # import_sipp called once with the saved CSV path
    mock_trader.import_sipp.assert_called_once()
    saved_path = Path(mock_trader.import_sipp.call_args.args[0])
    assert saved_path == tmp_path / "SIPP" / "merged.csv"
    # the uploaded bytes were persisted
    assert saved_path.read_bytes() == _CSV
    mock_trader.get_portfolio.assert_called_once()


def test_non_csv_is_rejected_without_import(mocked_import):
    mock_trader, mock_portfolio, _ = mocked_import
    resp = _upload(filename="portfolio.txt")

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_not_called()
    mock_portfolio.default_portfolio_context.assert_called_once()


def test_import_failure_returns_400(mocked_import):
    mock_trader, _, _ = mocked_import
    mock_trader.import_sipp.side_effect = ValueError("bad row")
    resp = _upload()

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_called_once()


def test_missing_columns_returns_400(mocked_import):
    from app.agents.trader.trader_agent import SippImportError

    mock_trader, _, _ = mocked_import
    mock_trader.import_sipp.side_effect = SippImportError(
        "CSV is missing required columns: Quantity"
    )
    resp = _upload()

    assert resp.status_code == 400
    mock_trader.import_sipp.assert_called_once()


def test_forbidden_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post("/import-sipp", files={"file": ("merged.csv", _CSV, "text/csv")})
    assert resp.status_code == 403
