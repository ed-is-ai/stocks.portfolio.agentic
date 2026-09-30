"""Tests for the narrow "set stop" write and its guarded route.

The write records a stop on a holding's latest replayed BUY only, and only
when the holding is currently held with no recorded stop: every other trade
row, cash flow and portfolio must stay byte-identical -- unlike the Adjust
dialog's correction, which replaces the ticker's whole trade history. The
route re-evaluates the Strategy through ``recommend`` and accepts only the
stop level it returns for that holding.
"""

from __future__ import annotations

import html
import inspect
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.agents.trader import trader_agent as agent_module
from app.agents.trader.trader_agent import (
    StopRefusedError,
    TraderAgent,
    stop_refusal,
)
from app.api.app import app
from app.api.dependencies import (
    get_portfolio_recommendation_service,
    get_portfolio_service,
    get_trader_service,
)
from app.core.ticker_identity import AliasFileUnreadableError
from app.api.routes.portfolios import set_position_stop
from app.schemas.portfolio_recommendation import NO_ASSIGNMENT, EvaluationUnavailable
from app.schemas.trade import Position
from app.services.gbp_valuation_service import GbpValuationService
from app.services.portfolio_agent_view import RecommendationOutcome
from app.services.portfolio_service import PortfolioService
from app.services.trader_service import TraderService
from tests.test_portfolio_service import _NoFxValuation
from tests.test_risk_engine import _kind, _run
from tests._stop_helpers import result, stop_level

client = TestClient(app)
_AUTH = {"X-Auth-Token": "s3cret"}


def _rows(db_path: Path, table: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    finally:
        conn.close()


def _dump(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    return {t: _rows(db_path, t) for t in ("trades", "cash_flows", "portfolios")}


def _stops(db_path: Path) -> dict[int, float | None]:
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT id, stop_loss FROM trades").fetchall())
    finally:
        conn.close()


def _insert_buys(db_path: Path, pid: int, rows: list[tuple[str, str]]) -> None:
    """Insert raw ``(ticker, date)`` BUY rows, bypassing date normalisation."""
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO trades (ticker, action, shares, price, date, notes,"
        " portfolio_id) VALUES (?, 'BUY', 1, 100, ?, '', ?)",
        [(ticker, day, pid) for ticker, day in rows],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def agent(tmp_path: Path) -> TraderAgent:
    return TraderAgent(db_path=tmp_path / "trades.db")


@pytest.fixture
def seeded(agent: TraderAgent) -> dict[str, Any]:
    """A portfolio holding AAA over three unstopped BUYs and a SELL, BBB, CCC
    (stopped), a closed DDD, and another portfolio's AAA, with opening cash."""
    pid = agent.create_portfolio("SIPP", opening_cash=1000.0).id
    other = agent.create_portfolio("ISA").id
    agent.record_buy("AAA", 5, 100.0, "2026-01-02", portfolio_id=pid)
    latest = agent.record_buy("AAA", 5, 100.0, "2026-03-02", portfolio_id=pid)
    # Recorded out of date order: must not be taken as the latest BUY.
    agent.record_buy("AAA", 5, 100.0, "2026-02-02", portfolio_id=pid)
    agent.record_sell("AAA", 2, 110.0, "2026-04-01", portfolio_id=pid)
    agent.record_buy("BBB", 1, 10.0, "2026-05-01", portfolio_id=pid)
    agent.record_buy("CCC", 1, 10.0, "2026-05-01", stop_loss=8.0, portfolio_id=pid)
    agent.record_buy("CCC", 1, 10.0, "2026-06-01", portfolio_id=pid)
    agent.record_buy("DDD", 1, 10.0, "2026-05-01", portfolio_id=pid)
    agent.record_sell("DDD", 1, 12.0, "2026-06-01", portfolio_id=pid)
    agent.record_buy("AAA", 1, 100.0, "2026-06-01", portfolio_id=other)
    return {"pid": pid, "other": other, "latest": latest.id}


def test_set_stop_updates_only_the_latest_buy(
    agent: TraderAgent, seeded: dict[str, Any]
) -> None:
    db_path = agent.db_path
    before = _dump(db_path)
    revision = agent.get_trade_revision(seeded["pid"])
    other_revision = agent.get_trade_revision(seeded["other"])

    agent.set_latest_buy_stop(seeded["pid"], "AAA", 95.0)

    after = _dump(db_path)
    assert after["cash_flows"] == before["cash_flows"]
    assert after["portfolios"] == before["portfolios"]
    changed = [
        (old, new) for old, new in zip(before["trades"], after["trades"]) if old != new
    ]
    assert len(after["trades"]) == len(before["trades"])
    assert [new[0] for _, new in changed] == [seeded["latest"]]
    assert _stops(db_path)[seeded["latest"]] == 95.0
    assert agent.get_trade_revision(seeded["pid"]) == revision + 1
    assert agent.get_trade_revision(seeded["other"]) == other_revision


def test_replayed_stop_is_evidence_for_the_risk_coach(
    agent: TraderAgent, seeded: dict[str, Any]
) -> None:
    def positions() -> list[Any]:
        held = agent.get_portfolio({"BBB": 10.0}, portfolio_id=seeded["pid"])
        return [p for p in held if p.ticker == "BBB"]

    assert _kind(_run(positions()), "no_stop")

    agent.set_latest_buy_stop(seeded["pid"], "BBB", 9.0)

    [bbb] = positions()
    assert bbb.stop_loss == 9.0
    assert not _kind(_run([bbb]), "no_stop")


def test_replay_reports_the_new_stop(
    agent: TraderAgent, seeded: dict[str, Any]
) -> None:
    agent.set_latest_buy_stop(seeded["pid"], "AAA", 95.0)

    [aaa] = [
        p for p in agent.get_portfolio(portfolio_id=seeded["pid"]) if p.ticker == "AAA"
    ]
    assert aaa.stop_loss == 95.0


def test_same_day_buys_follow_replay_order(agent: TraderAgent) -> None:
    """Within a date the replay processes the highest ``source_row_index``
    first, so the lowest index is the latest BUY."""
    pid = agent.create_portfolio("SIPP").id
    conn = sqlite3.connect(agent.db_path)
    conn.executemany(
        "INSERT INTO trades (ticker, action, shares, price, date, notes,"
        " portfolio_id, source_row_index, idempotency_key)"
        " VALUES ('AAA', 'BUY', 1, 100, '2026-01-02', '', ?, ?, ?)",
        [(pid, 0, "k0"), (pid, 5, "k5")],
    )
    conn.commit()
    conn.close()

    agent.set_latest_buy_stop(pid, "AAA", 95.0)

    conn = sqlite3.connect(agent.db_path)
    rows = dict(conn.execute("SELECT source_row_index, stop_loss FROM trades"))
    conn.close()
    assert rows == {0: 95.0, 5: None}
    [aaa] = agent.get_portfolio(portfolio_id=pid)
    assert aaa.stop_loss == 95.0


def test_legacy_non_iso_buy_is_skipped_like_the_replay(agent: TraderAgent) -> None:
    """A DD/MM/YYYY row sorts last as text, but the replay skips it."""
    pid = agent.create_portfolio("SIPP").id
    _insert_buys(agent.db_path, pid, [("AAA", "2026-01-02"), ("AAA", "31/12/2026")])
    ids = {row[5]: row[0] for row in _rows(agent.db_path, "trades")}

    agent.set_latest_buy_stop(pid, "AAA", 95.0)

    assert _stops(agent.db_path) == {ids["2026-01-02"]: 95.0, ids["31/12/2026"]: None}
    [aaa] = agent.get_portfolio(portfolio_id=pid)
    assert aaa.stop_loss == 95.0


def test_set_stop_matches_alias_spellings(
    agent: TraderAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    aliases = {"OLD": "NEW"}
    monkeypatch.setattr(agent_module, "load_aliases", lambda: aliases)
    pid = agent.create_portfolio("SIPP").id
    trade = agent.record_buy("OLD", 1, 100.0, "2026-01-02", portfolio_id=pid)

    agent.set_latest_buy_stop(pid, "NEW", 95.0)

    assert trade.id is not None
    assert _stops(agent.db_path)[trade.id] == 95.0


def test_set_stop_never_overwrites_a_recorded_stop(
    agent: TraderAgent, seeded: dict[str, Any]
) -> None:
    """CCC's earlier BUY carries a stop the replay reports: refuse, no write."""
    before = _dump(agent.db_path)

    with pytest.raises(StopRefusedError) as refused:
        agent.set_latest_buy_stop(seeded["pid"], "CCC", 9.5)

    assert str(refused.value) == (
        "A stop is already recorded for CCC; edit it with Adjust."
    )
    assert _dump(agent.db_path) == before


@pytest.mark.parametrize("ticker", ["ZZZ", "DDD"])
def test_set_stop_requires_a_current_holding(
    agent: TraderAgent, seeded: dict[str, Any], ticker: str
) -> None:
    before = _dump(agent.db_path)

    with pytest.raises(StopRefusedError, match="Not currently held"):
        agent.set_latest_buy_stop(seeded["pid"], ticker, 9.0)
    with pytest.raises(StopRefusedError, match="Not currently held"):
        agent.set_latest_buy_stop(seeded["other"], "BBB", 9.0)

    assert _dump(agent.db_path) == before


# --- route (real PortfolioService / TraderAgent stack) -------------------------


@pytest.fixture
def stack(
    monkeypatch: pytest.MonkeyPatch, agent: TraderAgent, seeded: dict[str, Any]
) -> Iterator[SimpleNamespace]:
    """Auth + the real tab stack on a temp DB, and a recommendation service
    whose outcome each test sets: by default the Strategy's stop for AAA is
    95.0 (in GBP, the holding's price unit)."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    agent.save_price_cache({"AAA": 100.0}, {"AAA": (100.0, "GBP")})
    trader = TraderService(agent)
    service = PortfolioService(
        trader, gbp_valuation=cast(GbpValuationService, _NoFxValuation())
    )
    monkeypatch.setattr(service, "load_analysis", list)
    outcome: dict[str, Callable[[int], RecommendationOutcome]] = {
        "recommend": lambda _pid: result(AAA=stop_level("95.0"))
    }
    recommendations = SimpleNamespace(recommend=lambda pid: outcome["recommend"](pid))
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: service
    app.dependency_overrides[get_portfolio_recommendation_service] = lambda: (
        recommendations
    )
    try:
        yield SimpleNamespace(service=service, outcome=outcome)
    finally:
        app.dependency_overrides.clear()


def _post(pid: int, ticker: str, value: str | None, **headers: str) -> Any:
    data = {} if value is None else {"stop_loss": value}
    return client.post(
        f"/portfolios/{pid}/positions/{ticker}/stop",
        data=data,
        headers=headers or _AUTH,
    )


def _warning(body: str) -> str:
    """The re-rendered tab's warning text (the alert's message span)."""
    alert = body.split('class="alert alert-warning', 1)[1]
    return html.unescape(alert.split("<span>", 1)[1].split("</span>", 1)[0])


def test_use_records_the_suggestion_and_rerenders_the_recorded_stop(
    stack: SimpleNamespace, agent: TraderAgent, seeded: dict[str, Any]
) -> None:
    pid = seeded["pid"]
    tab = client.get(f"/partials/portfolio?portfolio_id={pid}").text
    assert f'id="agent-{pid}-stop-AAA"' in tab

    resp = _post(pid, "AAA", "95.0")

    assert resp.status_code == 200
    assert _stops(agent.db_path)[seeded["latest"]] == 95.0
    assert '<span class="neg">£95.00</span>' in resp.text
    assert f'id="agent-{pid}-stop-AAA"' not in resp.text
    assert 'class="alert alert-warning' not in resp.text


@pytest.mark.parametrize("value", [None, "", "abc", "0", "-5", "nan", "inf"])
def test_route_rejects_invalid_stop_without_writing(
    stack: SimpleNamespace, agent: TraderAgent, seeded: dict[str, Any], value: Any
) -> None:
    before = _dump(agent.db_path)

    resp = _post(seeded["pid"], "AAA", value)

    assert resp.status_code == 422
    assert _warning(resp.text) == "Enter a positive stop price."
    assert _dump(agent.db_path) == before


@pytest.mark.parametrize(
    ("ticker", "value", "message"),
    [
        ("AAA", "94.99", "The suggestion changed; reload the Portfolio tab."),
        ("CCC", "9.2", "A stop is already recorded for CCC; edit it with Adjust."),
        ("ZZZ", "95.0", "Not currently held."),
        ("DDD", "95.0", "Not currently held."),
    ],
)
def test_route_refusals_are_visible_409s_without_writing(
    stack: SimpleNamespace,
    agent: TraderAgent,
    seeded: dict[str, Any],
    ticker: str,
    value: str,
    message: str,
) -> None:
    before = _dump(agent.db_path)

    resp = _post(seeded["pid"], ticker, value)

    assert resp.status_code == 409
    assert _warning(resp.text) == message
    # The warning arrives inside the re-rendered Portfolio tab.
    assert 'id="portfolioSelect"' in resp.text
    assert _dump(agent.db_path) == before


def test_route_refuses_a_race_the_write_transaction_catches(
    stack: SimpleNamespace,
    agent: TraderAgent,
    seeded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop recorded after the tab context was built is still not
    overwritten: the transaction re-checks and the refusal is a 409."""
    real = TraderAgent.set_latest_buy_stop

    def racing(self: TraderAgent, pid: int, ticker: str, stop: float) -> None:
        conn = sqlite3.connect(agent.db_path)
        conn.execute(
            "UPDATE trades SET stop_loss = 50 WHERE id = ?", (seeded["latest"],)
        )
        conn.commit()
        conn.close()
        real(self, pid, ticker, stop)

    monkeypatch.setattr(TraderAgent, "set_latest_buy_stop", racing)

    resp = _post(seeded["pid"], "AAA", "95.0")

    assert resp.status_code == 409
    assert "already recorded for AAA" in _warning(resp.text)
    assert _stops(agent.db_path)[seeded["latest"]] == 50.0


def test_route_alias_file_failure_is_a_visible_409(
    stack: SimpleNamespace,
    agent: TraderAgent,
    seeded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(*_args: Any) -> None:
        raise AliasFileUnreadableError("bad json")

    monkeypatch.setattr(TraderAgent, "set_latest_buy_stop", unreadable)
    before = _dump(agent.db_path)

    resp = _post(seeded["pid"], "AAA", "95.0")

    assert resp.status_code == 409
    assert "could not be recorded" in _warning(resp.text)
    assert _dump(agent.db_path) == before


def test_route_accepts_a_ticker_with_a_slash(
    stack: SimpleNamespace,
    agent: TraderAgent,
    seeded: dict[str, Any],
) -> None:
    pid = seeded["pid"]
    trade = agent.record_buy("BRK/B", 1, 100.0, "2026-07-01", portfolio_id=pid)
    stack.outcome["recommend"] = lambda _pid: result(**{"BRK/B": stop_level("96")})

    resp = _post(pid, "BRK/B", "96")

    assert resp.status_code == 200
    assert trade.id is not None
    assert _stops(agent.db_path)[trade.id] == 96.0


def _boom(_pid: int) -> RecommendationOutcome:
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    ("recommend", "message"),
    [
        (lambda _pid: NO_ASSIGNMENT, "No Strategy assigned"),
        (
            lambda _pid: EvaluationUnavailable(reason="No published scan artifact."),
            "No published scan artifact.",
        ),
        (lambda _pid: result(), "No stop level from the Strategy"),
        (
            lambda _pid: result(AAA=stop_level(None, summary="Needs history.")),
            "Needs history.",
        ),
        (
            lambda _pid: result(AAA=stop_level("95.0", currency="GBp")),
            "Strategy prices are in a different unit",
        ),
        (
            lambda _pid: result("stale", AAA=stop_level("95.0")),
            "The Strategy's data is stale; run the pipeline first.",
        ),
        (_boom, "The Strategy could not be evaluated; reload the Portfolio tab."),
    ],
)
def test_route_refuses_with_the_reason_there_is_no_usable_level(
    stack: SimpleNamespace,
    agent: TraderAgent,
    seeded: dict[str, Any],
    recommend: Callable[[int], RecommendationOutcome],
    message: str,
) -> None:
    """The level is recomputed through ``recommend``; no usable level (no
    Strategy, a failed or stale evaluation, none returned, another unit)
    records nothing and says why."""
    stack.outcome["recommend"] = recommend
    before = _dump(agent.db_path)

    resp = _post(seeded["pid"], "AAA", "95.0")

    assert resp.status_code == 409
    assert _warning(resp.text) == message
    assert _dump(agent.db_path) == before


def test_route_runs_off_the_event_loop() -> None:
    """A plain ``def``: FastAPI runs the synchronous evaluation in its
    threadpool rather than blocking the event loop."""
    assert not inspect.iscoroutinefunction(set_position_stop)


def test_a_recorded_zero_stop_is_refused() -> None:
    position = Position(
        ticker="AAA", shares=1.0, avg_cost=1.0, total_cost=1.0, stop_loss=0.0
    )

    assert stop_refusal(position) == (
        "A stop is already recorded for AAA; edit it with Adjust."
    )


def test_route_compares_small_levels_at_full_precision(
    stack: SimpleNamespace, agent: TraderAgent, seeded: dict[str, Any]
) -> None:
    """A level shown as 0.0345 is posted and recorded whole."""
    pid = seeded["pid"]
    trade = agent.record_buy("PNY", 1, 0.0375, "2026-07-01", portfolio_id=pid)
    level = "0.0345000000000001"
    stack.outcome["recommend"] = lambda _pid: result(PNY=stop_level(level))

    assert _post(pid, "PNY", "0.0345").status_code == 409
    resp = _post(pid, "PNY", level)

    assert resp.status_code == 200
    assert trade.id is not None
    assert _stops(agent.db_path)[trade.id] == float(level)


def test_route_rejects_cross_site_requests_without_a_token(
    monkeypatch: pytest.MonkeyPatch, agent: TraderAgent, seeded: dict[str, Any]
) -> None:
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    app.dependency_overrides[get_trader_service] = lambda: TraderService(agent)
    before = _dump(agent.db_path)
    try:
        resp = _post(seeded["pid"], "AAA", "95.0", **{"Sec-Fetch-Site": "cross-site"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert _dump(agent.db_path) == before
