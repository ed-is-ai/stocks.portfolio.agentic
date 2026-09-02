"""Tests for the Strategy assign/clear routes and modal partial (#440).

Follows the ``tests/test_portfolios_routes.py`` pattern: TestClient +
``app.dependency_overrides`` + ``APP_AUTH_TOKEN``. The modal partial is
rendered with the real Jinja template to catch markup errors.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_portfolio_recommendation_service,
    get_portfolio_service,
    get_strategy_assignment_service,
    get_trader_service,
)
from app.repositories import db
from app.repositories.portfolio_strategies_repo import (
    PortfolioStrategiesRepository,
)
from app.schemas.trade import Portfolio
from app.services.backtest.skill_discovery import StrategyDiscoveryResultV1
from app.services import strategy_assignment_service as svc_module
from app.services.portfolio_service import PortfolioService
from app.services.strategy_assignment_service import StrategyAssignmentService
from tests.test_strategy_assignment_service import _discovery_result

client = TestClient(app)
_AUTH = {"X-Auth-Token": "s3cret"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.execute(
        "INSERT INTO portfolios (id, name, created_at) VALUES (7, 'SIPP', 'now')"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def assignment_service(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StrategyAssignmentService:
    repo = PortfolioStrategiesRepository(db.make_connect(lambda: db_path))
    service = StrategyAssignmentService(
        repo,
        skills_root=tmp_path / "skills",
        analysis_path=tmp_path / "analysis.json",
    )
    monkeypatch.setattr(
        svc_module, "discover_strategies", lambda root: _discovery_result()
    )
    return service


@pytest.fixture
def mocked(
    monkeypatch: pytest.MonkeyPatch, assignment_service: StrategyAssignmentService
) -> Iterator[dict[str, Any]]:
    """Auth + stubbed partial rendering + overridden dependencies."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    captured: dict[str, Any] = {}

    def fake_template_response(
        _request: object, name: str, context: dict | None = None, **kwargs: object
    ) -> HTMLResponse:
        captured["name"] = name
        captured["context"] = context or {}
        status = kwargs.get("status_code", 200)
        return HTMLResponse("ok", status_code=int(status))  # type: ignore[arg-type]

    monkeypatch.setattr(
        "app.api.routes.portfolios.templates.TemplateResponse",
        fake_template_response,
    )
    mock_trader = MagicMock()
    mock_portfolio = MagicMock()
    mock_portfolio.default_portfolio_context.return_value = {}
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    app.dependency_overrides[get_strategy_assignment_service] = lambda: (
        assignment_service
    )
    try:
        yield {"captured": captured, "service": assignment_service}
    finally:
        app.dependency_overrides.clear()


def test_assign_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post(
        "/portfolios/7/strategy",
        data={"strategy_id": "alpha"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


def test_clear_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post(
        "/portfolios/7/strategy/clear",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


def test_assign_success_rerenders_partial(
    mocked: dict[str, Any], db_path: Path
) -> None:
    resp = client.post(
        "/portfolios/7/strategy", data={"strategy_id": "alpha"}, headers=_AUTH
    )
    assert resp.status_code == 200
    assert mocked["captured"]["name"] == "_portfolio.html"
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT strategy_id FROM portfolio_strategies WHERE portfolio_id = 7"
    ).fetchone()
    conn.close()
    assert row == ("alpha",)


def test_assign_unknown_strategy_renders_warning_not_500(
    mocked: dict[str, Any], db_path: Path
) -> None:
    resp = client.post(
        "/portfolios/7/strategy", data={"strategy_id": "ghost"}, headers=_AUTH
    )
    assert resp.status_code == 200
    context = mocked["captured"]["context"]
    assert "Unknown Strategy" in context["warning_message"]
    # The stored assignment is untouched (there was none).
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT * FROM portfolio_strategies").fetchall()
    conn.close()
    assert rows == []


def test_clear_is_idempotent(mocked: dict[str, Any]) -> None:
    first = client.post("/portfolios/7/strategy/clear", headers=_AUTH)
    second = client.post("/portfolios/7/strategy/clear", headers=_AUTH)
    assert first.status_code == 200
    assert second.status_code == 200


def test_strategy_assign_partial_renders_real_template(
    monkeypatch: pytest.MonkeyPatch, assignment_service: StrategyAssignmentService
) -> None:
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    app.dependency_overrides[get_strategy_assignment_service] = lambda: (
        assignment_service
    )
    try:
        resp = client.get("/partials/strategy-assign?portfolio_id=7")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    # Real template render: choices, default-parameter chips, and the
    # missing-artifact freshness warning are all present.
    assert "Alpha" in resp.text
    assert "lookback=20" in resp.text
    assert "Scan data is missing" in resp.text
    assert 'hx-post="/portfolios/7/strategy"' in resp.text


def test_strategy_assign_partial_shows_unavailable_assignment(
    monkeypatch: pytest.MonkeyPatch, assignment_service: StrategyAssignmentService
) -> None:
    assignment_service._repo.upsert(7, "ghost", {"lookback": 20})
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    app.dependency_overrides[get_strategy_assignment_service] = lambda: (
        assignment_service
    )
    try:
        resp = client.get("/partials/strategy-assign?portfolio_id=7")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "unavailable" in resp.text
    assert "retained until cleared" in resp.text


def test_portfolio_partial_strategy_control_gates_recommendations(
    monkeypatch: pytest.MonkeyPatch, assignment_service: StrategyAssignmentService
) -> None:
    """A portfolio with no assignment renders the new control only (#440.7)."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    mock_trader = MagicMock()
    mock_trader.list_portfolios.return_value = [
        Portfolio(id=7, name="SIPP", created_at="2024-01-01")
    ]
    mock_trader.get_trade_history.return_value = []
    mock_trader.get_cash_flows.return_value = []
    mock_trader.list_reconciliation_issues.return_value = []
    mock_trader.list_cash_balances.return_value = []
    mock_trader.get_cash_balance.return_value = 100.0
    mock_trader.snapshot_history.return_value = []
    mock_trader.load_price_cache.return_value = ({}, None, {})
    portfolio_service = PortfolioService(
        mock_trader, assignment_service=assignment_service
    )
    app.dependency_overrides[get_portfolio_service] = lambda: portfolio_service
    app.dependency_overrides[get_strategy_assignment_service] = lambda: (
        assignment_service
    )
    try:
        unassigned = client.get("/partials/portfolio?portfolio_id=7")
        assignment_service._repo.upsert(7, "alpha", {"lookback": 20})
        assigned = client.get("/partials/portfolio?portfolio_id=7")
        assignment_service._repo.upsert(7, "ghost", {"lookback": 20})
        unavailable = client.get("/partials/portfolio?portfolio_id=7")
    finally:
        app.dependency_overrides.clear()
    assert unassigned.status_code == 200
    assert "No Strategy" not in unassigned.text
    assert "Select strategy" in unassigned.text
    assert "Select an available strategy to view recommendations" in unassigned.text
    assert 'hx-get="/portfolios/7/recommendations"' not in unassigned.text
    assert "/partials/strategy-assign?portfolio_id=7" in unassigned.text
    assert "Scan data is missing" not in unassigned.text

    assert assigned.status_code == 200
    assert "Change strategy" in assigned.text
    assert 'aria-label="Selected strategy: Alpha"' in assigned.text
    assert assigned.text.index("Change strategy") < assigned.text.index(
        'aria-label="Selected strategy: Alpha"'
    )
    assert 'hx-get="/portfolios/7/recommendations"' in assigned.text

    assert unavailable.status_code == 200
    assert "Repair strategy" in unavailable.text
    assert 'aria-label="Selected strategy unavailable: ghost"' in unavailable.text
    assert "Select an available strategy to view recommendations" in unavailable.text
    assert 'hx-get="/portfolios/7/recommendations"' not in unavailable.text
    assert "Scan data is missing" not in unavailable.text
    # The tmp analysis artifact is absent, so the non-blocking freshness
    # warning shows while assignment remains available.
    assert "Scan data is missing" in assigned.text


def test_clear_removes_existing_assignment(
    mocked: dict[str, Any], db_path: Path
) -> None:
    """Clearing a real assignment deletes the row and re-renders (#440)."""
    mocked["service"]._repo.upsert(7, "alpha", {"lookback": 20})
    resp = client.post("/portfolios/7/strategy/clear", headers=_AUTH)
    assert resp.status_code == 200
    assert mocked["captured"]["name"] == "_portfolio.html"
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT * FROM portfolio_strategies").fetchall()
    conn.close()
    assert rows == []


def test_assign_incompatible_strategy_renders_warning_and_keeps_assignment(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    tmp_path: Path,
) -> None:
    """A skill whose own defaults fail validation renders a warning (200)
    and leaves the stored assignment untouched — never a 500 (#440)."""
    from tests.test_strategy_assignment_service import _descriptor

    repo = PortfolioStrategiesRepository(db.make_connect(lambda: db_path))
    service = StrategyAssignmentService(
        repo,
        skills_root=tmp_path / "skills",
        analysis_path=tmp_path / "analysis.json",
    )
    repo.upsert(7, "alpha", {"lookback": 20})
    broken = _descriptor("broken", default_parameters={"lookback": 0})
    monkeypatch.setattr(
        svc_module,
        "discover_strategies",
        lambda root: StrategyDiscoveryResultV1(strategies=(broken,), warnings=()),
    )
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    captured: dict[str, Any] = {}
    mock_portfolio = MagicMock()
    mock_portfolio.default_portfolio_context.return_value = {}

    def fake_template_response(
        _request: object, name: str, context: dict | None = None, **_: object
    ) -> HTMLResponse:
        captured["name"] = name
        captured["context"] = context or {}
        return HTMLResponse("ok")

    monkeypatch.setattr(
        "app.api.routes.portfolios.templates.TemplateResponse",
        fake_template_response,
    )
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    app.dependency_overrides[get_strategy_assignment_service] = lambda: service
    try:
        resp = client.post(
            "/portfolios/7/strategy", data={"strategy_id": "broken"}, headers=_AUTH
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "Could not assign Strategy" in captured["context"]["warning_message"]
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT strategy_id FROM portfolio_strategies WHERE portfolio_id = 7"
    ).fetchone()
    conn.close()
    assert row == ("alpha",)  # stored assignment untouched


def test_assign_to_missing_portfolio_is_not_500(
    mocked: dict[str, Any], db_path: Path
) -> None:
    """A foreign-key violation (portfolio deleted mid-flight) renders a
    visible warning instead of an unhandled 500 (#440)."""
    resp = client.post(
        "/portfolios/999/strategy", data={"strategy_id": "alpha"}, headers=_AUTH
    )
    assert resp.status_code == 404
    assert "no longer exists" in mocked["captured"]["context"]["warning_message"]


def test_routes_never_launch_backtest_scan_email_or_trade() -> None:
    """Mechanical guard for the story's core 'Never' boundary (#440).

    Checks the module's imports (not docstrings, which legitimately mention
    the words): no backtest, email, or alert machinery may be imported.
    """
    import ast
    import inspect

    source = inspect.getsource(sys.modules["app.api.routes.portfolios"])
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for module in imported:
        assert not module.startswith(("app.services.backtest", "app.agents.alert")), (
            f"forbidden import: {module}"
        )
        assert "smtplib" not in module, f"forbidden import: {module}"


def test_portfolio_partial_renders_stale_banner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    assignment_service: StrategyAssignmentService,
) -> None:
    """A >24h-old artifact shows the stale banner; assignment still works."""
    from datetime import UTC, datetime, timedelta

    from app.schemas.analysis_artifact import build_analysis_payload

    stale = datetime.now(UTC) - timedelta(hours=25)
    payload = build_analysis_payload([], run_id="r1", generated_at=stale)
    (tmp_path / "analysis.json").write_text(json.dumps(payload))
    assignment_service._repo.upsert(7, "alpha", {"lookback": 20})

    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    mock_trader = MagicMock()
    mock_trader.list_portfolios.return_value = [
        Portfolio(id=7, name="SIPP", created_at="2024-01-01")
    ]
    mock_trader.get_trade_history.return_value = []
    mock_trader.get_cash_flows.return_value = []
    mock_trader.list_reconciliation_issues.return_value = []
    mock_trader.list_cash_balances.return_value = []
    mock_trader.get_cash_balance.return_value = 100.0
    mock_trader.snapshot_history.return_value = []
    mock_trader.load_price_cache.return_value = ({}, None, {})
    portfolio_service = PortfolioService(
        mock_trader, assignment_service=assignment_service
    )
    app.dependency_overrides[get_portfolio_service] = lambda: portfolio_service
    app.dependency_overrides[get_strategy_assignment_service] = lambda: (
        assignment_service
    )
    try:
        resp = client.get("/partials/portfolio?portfolio_id=7")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "more than 24 hours old" in resp.text


def test_strategy_assign_partial_shows_recommendation_support_badges(
    monkeypatch: pytest.MonkeyPatch, assignment_service: StrategyAssignmentService
) -> None:
    """Each choice carries its current recommendation-support badge (#471)."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    support = MagicMock()
    support.strategy_support.return_value = {
        "alpha": "supported",
        "beta": "backtest_only",
    }
    app.dependency_overrides[get_strategy_assignment_service] = lambda: (
        assignment_service
    )
    app.dependency_overrides[get_portfolio_recommendation_service] = lambda: support
    try:
        resp = client.get("/partials/strategy-assign?portfolio_id=7")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "Recommendations: supported" in resp.text
    assert "Recommendations: backtest only" in resp.text


def test_strategy_assign_partial_renders_when_support_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, assignment_service: StrategyAssignmentService
) -> None:
    """Support lookup is fail-soft — the modal still renders (#471)."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    support = MagicMock()
    support.strategy_support.side_effect = RuntimeError("boom")
    app.dependency_overrides[get_strategy_assignment_service] = lambda: (
        assignment_service
    )
    app.dependency_overrides[get_portfolio_recommendation_service] = lambda: support
    try:
        resp = client.get("/partials/strategy-assign?portfolio_id=7")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "Alpha" in resp.text
    assert "Recommendations:" not in resp.text
