"""Real-stack tests for the Portfolio Risk Coach partial (GH-16)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.trader.trader_agent import TraderAgent
from app.api.app import app
from app.api.dependencies import get_portfolio_service, get_trader_service
from app.schemas.record import StockRecord
from app.schemas.scan import StockAnalysis, StockScan
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService

client = TestClient(app)


def _record(ticker: str, price: float, stop: float, sector: str) -> StockRecord:
    scan = StockScan(
        ticker=ticker,
        as_of="2026-09-25",
        price=price,
        volume=1_000_000,
        rel_volume=1.0,
        high_52w=price,
        low_52w=price / 2,
        pct_from_52w_high=0.0,
        pct_change_week=1.0,
        sector=sector,
        currency="GBP",
    )
    record = StockRecord.model_validate(scan.model_dump())
    record.analysis = StockAnalysis(
        score=5, stage="Stage 2", stop_loss=stop, summary="test"
    )
    return record


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    # AAA: 10 @ £100 with a £85 BUY stop -> £150 at risk (7.5% of £2,000).
    agent.record_buy("AAA", 10, 90.0, "2026-01-02", stop_loss=85.0, portfolio_id=pf.id)
    # BBB has no price anywhere -> unpriced; no analysis -> unknown sector.
    agent.record_buy("BBB", 5, 10.0, "2026-01-02", portfolio_id=pf.id)
    agent.set_cash_balance(1000.0, pf.id)
    agent.save_price_cache({"AAA": 100.0}, {"AAA": (100.0, "GBP")})
    trader = TraderService(agent)
    service = PortfolioService(trader)
    monkeypatch.setattr(
        service, "load_analysis", lambda: [_record("AAA", 100.0, 80.0, "Energy")]
    )
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: service
    try:
        yield agent, pf.id
    finally:
        app.dependency_overrides.clear()


def _dump(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return list(conn.iterdump())


def test_risk_partial_renders_policy_findings_and_inputs(stack) -> None:
    _, pid = stack
    resp = client.get("/partials/portfolio/risk", params={"portfolio_id": pid})
    assert resp.status_code == 200
    body = resp.text
    # Policy limits are visible, not hidden in a tooltip.
    assert "max position 20%" in body
    assert "max sector 35%" in body
    assert "max capital at risk to stops 6%" in body
    assert "Confidence: limited" in body
    # AAA is 50% of £2,000 and 7.5% is at risk to its BUY-trade stop.
    assert 'data-risk-kind="position_concentration"' in body
    assert 'data-risk-kind="sector_concentration"' in body
    assert "Capital at risk to stops is 7.5%" in body
    assert "AAA (BUY trade stop 85.00 GBP)" in body
    assert "£150.00 (7.5%)" in body
    assert "£2,000.00" in body
    assert 'data-risk-kind="unpriced"' in body
    assert 'data-risk-kind="unknown_sector"' in body
    # High findings come before medium/info ones.
    assert body.index("position_concentration") < body.index('"unpriced"')


def test_risk_partial_performs_no_writes(stack) -> None:
    agent, pid = stack
    before = _dump(agent.db_path)
    for params in ({"portfolio_id": pid}, {"portfolio_id": ""}, {}):
        assert client.get("/partials/portfolio/risk", params=params).status_code == 200
    assert _dump(agent.db_path) == before


def test_unknown_portfolio_falls_back_to_active(stack) -> None:
    resp = client.get("/partials/portfolio/risk", params={"portfolio_id": "999"})
    assert resp.status_code == 200
    assert "Capital at risk to stops is 7.5%" in resp.text


def test_portfolio_tab_lazy_loads_risk_panel(stack) -> None:
    _, pid = stack
    resp = client.get("/partials/portfolio", params={"portfolio_id": pid})
    assert resp.status_code == 200
    assert f'hx-get="/partials/portfolio/risk?portfolio_id={pid}"' in resp.text
    assert 'hx-trigger="load"' in resp.text
