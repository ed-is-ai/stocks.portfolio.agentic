"""Tests for the portfolio value-chart range selector + fragment (#421)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.trader.trader_agent import TraderAgent
from app.api.app import app
from app.api.dependencies import get_portfolio_service, get_trader_service
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService


def _now_iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def stack(tmp_path: Path):
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    # Snapshots spanning ~2 years: one 20 months ago, then monthly for a year.
    agent._snapshots.append(pf.id, _now_iso(600), 1000.0, 900.0, 100.0)
    # Monthly snapshots from ~13 months ago to ~70 days ago — deliberately
    # none inside the 1M/3M windows so the "no data in range" path is
    # deterministic regardless of setup latency (#421).
    for m in range(12):
        agent._snapshots.append(
            pf.id, _now_iso(400 - m * 30), 1000.0 + m * 10, 900.0, 100.0
        )
    trader = TraderService(agent)
    service = PortfolioService(trader)
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: service
    try:
        yield agent, pf.id
    finally:
        app.dependency_overrides.clear()


client = TestClient(app)


@pytest.mark.parametrize("preset", ["1M", "3M", "12M", "3Y", "5Y"])
def test_chart_fragment_renders_card_for_each_preset(stack, preset: str) -> None:
    _, pid = stack
    resp = client.get(
        "/partials/portfolio/chart", params={"portfolio_id": pid, "range": preset}
    )
    assert resp.status_code == 200
    assert 'id="portfolio-chart-card"' in resp.text
    assert 'aria-pressed="true"' in resp.text


def test_bad_range_resolves_to_12m(stack) -> None:
    _, pid = stack
    resp = client.get(
        "/partials/portfolio/chart", params={"portfolio_id": pid, "range": "9Q"}
    )
    assert resp.status_code == 200
    # 12M button carries the active marker.
    assert 'aria-pressed="true"' in resp.text
    body = resp.text
    active_idx = body.index('aria-pressed="true"')
    assert "12M" in body[active_idx - 400 : active_idx + 400]


def test_12m_window_excludes_older_snapshots(stack) -> None:
    _, pid = stack
    twelve = client.get(
        "/partials/portfolio/chart", params={"portfolio_id": pid, "range": "12M"}
    ).text
    five = client.get(
        "/partials/portfolio/chart", params={"portfolio_id": pid, "range": "5Y"}
    ).text
    # The 20-month-old snapshot (value 1000.0, cost 900.0) is only in 5Y.
    assert five.count("1000") >= twelve.count("1000")
    assert 'id="portfolio-chart-card"' in twelve


def test_empty_window_shows_no_data_message(stack) -> None:
    # The 1M window has at most one recent snapshot -> under the 2-point bar.
    _, pid = stack
    resp = client.get(
        "/partials/portfolio/chart", params={"portfolio_id": pid, "range": "1M"}
    )
    assert resp.status_code == 200
    assert "No data in this range" in resp.text
    # Selector still present.
    assert 'hx-get="/partials/portfolio/chart"' in resp.text


def test_chart_fragment_remains_a_chart_card_without_dashboard_context(stack) -> None:
    _, pid = stack
    resp = client.get(
        "/partials/portfolio/chart", params={"portfolio_id": pid, "range": "12M"}
    )

    assert resp.status_code == 200
    assert 'id="portfolio-chart-card"' in resp.text
    assert 'class="portfolio-dashboard"' not in resp.text
    assert 'class="portfolio-chart-canvas"' in resp.text


def test_out_of_window_trade_has_no_marker(stack) -> None:
    agent, pid = stack
    old_date = (datetime.now(timezone.utc) - timedelta(days=500)).strftime("%Y-%m-%d")
    agent.record_buy("AAPL", 10, 100, old_date, portfolio_id=pid)
    resp = client.get(
        "/partials/portfolio/chart", params={"portfolio_id": pid, "range": "3M"}
    )
    assert resp.status_code == 200
    # No buy tooltip string for the out-of-window trade.
    assert "BUY 10 AAPL" not in resp.text


@pytest.fixture
def empty_stack(tmp_path: Path):
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    trader = TraderService(agent)
    service = PortfolioService(trader)
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: service
    try:
        yield agent
    finally:
        app.dependency_overrides.clear()


def test_chart_fragment_keeps_card_shell_with_no_portfolios(empty_stack) -> None:
    resp = client.get("/partials/portfolio/chart", params={"portfolio_id": ""})
    assert resp.status_code == 200
    # The card shell + selector survive an outerHTML swap even with no account.
    assert 'id="portfolio-chart-card"' in resp.text
    for preset in ("1M", "3M", "12M", "3Y", "5Y"):
        assert f'"range": "{preset}"' in resp.text


def test_chart_fragment_keeps_card_shell_with_unknown_id(stack) -> None:
    resp = client.get(
        "/partials/portfolio/chart", params={"portfolio_id": "9999", "range": "3M"}
    )
    assert resp.status_code == 200
    assert 'id="portfolio-chart-card"' in resp.text
    # All five range buttons are present.
    for preset in ("1M", "3M", "12M", "3Y", "5Y"):
        assert f'"range": "{preset}"' in resp.text


def test_full_portfolio_render_accepts_range_param(stack) -> None:
    agent, pid = stack
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    agent.record_buy("AAPL", 10, 100, recent, portfolio_id=pid)
    resp = client.get(
        "/partials/portfolio", params={"portfolio_id": pid, "range": "5Y"}
    )
    assert resp.status_code == 200
    assert 'id="portfolio-chart-card"' in resp.text
