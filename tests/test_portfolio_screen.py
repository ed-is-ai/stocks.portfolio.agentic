"""Contract tests for the simplified Portfolio screen (GH-130).

AD-3 (the trades sub-view is a full-shell page URL), AD-4 (retired pages
redirect, their partials are gone and nothing links to them), AD-5 (the
Risk check card renders ``risk_items`` and nothing else), plus the screen's
own acceptance: no "Loading…" on first paint, Clear only in the ⋯ menu, a
chart range that covers the data, Weight and Set stop. Built over
``tests.test_desk_service``'s ``tmp_path`` stack, so no real ledger is read.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_desk_service,
    get_portfolio_service,
    get_realised_pnl_service,
    get_trader_service,
)
from app.api.templating import templates
from app.schemas.trade import Position
from app.schemas.portfolio_risk import RiskFindingV1, RiskPolicyV1, RiskReportV1
from app.services.portfolio_service import (
    PortfolioService,
    holding_columns,
    risk_notes,
)
from app.services.realised_pnl_service import RealisedPnlService
from tests.test_desk_service import desk_stack  # noqa: F401 -- fixture
from tests.test_today_routes import _tab
from tests.test_views_index_freshness import _use_artifact

client = TestClient(app)


@pytest.fixture
def stack(desk_stack, monkeypatch, tmp_path) -> Iterator[SimpleNamespace]:  # noqa: F811
    """The Desk stack behind every Portfolio route."""
    _use_artifact(monkeypatch, tmp_path, datetime.now(UTC))
    overrides = {
        get_desk_service: desk_stack.desk,
        get_trader_service: lambda: desk_stack.trader,
        get_portfolio_service: lambda: desk_stack.service,
        get_realised_pnl_service: lambda: RealisedPnlService(
            desk_stack.trader, desk_stack.service
        ),
    }
    for dependency, override in overrides.items():
        monkeypatch.setitem(app.dependency_overrides, dependency, override)
    yield desk_stack


# --- AD-3 / AD-4: the trades sub-view and the retired pages ------------------


def test_trades_page_is_the_full_shell_with_its_partial(stack) -> None:
    plain = client.get("/portfolio/trades")
    htmx = client.get("/portfolio/trades", headers={"HX-Request": "true"})

    assert plain.status_code == 200 and plain.text == htmx.text
    loader = plain.text[plain.text.index('id="shell-loader"') :].split(">", 1)[0]
    assert 'hx-get="/partials/portfolio/trades"' in loader
    assert "active" in _tab(plain.text, "portfolio")
    assert "active" not in _tab(plain.text, "today")


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("/realised-pnl", "/portfolio/trades"),
        ("/realised-pnl?portfolio_id=2", "/portfolio/trades?portfolio_id=2"),
        ("/history", "/portfolio/trades"),
        ("/history?portfolio_id=3", "/portfolio/trades?portfolio_id=3"),
    ],
)
def test_retired_trade_pages_redirect_keeping_the_query(url: str, target: str) -> None:
    resp = client.get(url, follow_redirects=False)

    assert resp.status_code == 307
    assert resp.headers["location"] == target


@pytest.mark.parametrize("url", ["/partials/realised-pnl", "/partials/history"])
def test_retired_trade_partials_are_gone(url: str) -> None:
    assert client.get(url).status_code == 404


@pytest.mark.parametrize(
    ("query", "target"),
    [
        ("", "/partials/portfolio/trades"),
        ("?portfolio_id=1", "/partials/portfolio/trades?portfolio_id=1"),
    ],
)
def test_delete_trade_redirects_to_the_trades_partial(
    stack, monkeypatch: pytest.MonkeyPatch, query: str, target: str
) -> None:
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    trade = stack.trader.get_trade_history(portfolio_id=stack.pid)[0]

    resp = client.delete(
        f"/trades/{trade.id}{query}",
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == target


def test_trades_partial_holds_realised_pnl_then_trades(stack) -> None:
    body = client.get(
        "/partials/portfolio/trades", params={"portfolio_id": stack.pid}
    ).text

    assert body.index("Realised P&amp;L") < body.index(">Trades</h2>")
    nav = body.split('class="portfolio-views"', 1)[1].split("</nav>", 1)[0]
    assert 'data-portfolio-view="trades" aria-current="page"' in nav
    assert 'hx-get="/partials/portfolio" hx-push-url="/portfolio"' in nav
    assert "Loading" not in body
    # A trade's delete keeps the portfolio being viewed.
    assert f'?portfolio_id={stack.pid}"' in body.split('hx-delete="/trades/', 1)[1]


# --- The Holdings view --------------------------------------------------------


def _holdings(stack: SimpleNamespace) -> str:
    resp = client.get("/partials/portfolio", params={"portfolio_id": stack.pid})
    assert resp.status_code == 200
    return resp.text


def test_holdings_first_paint_has_no_loading_placeholder(stack) -> None:
    body = _holdings(stack)

    assert "Loading" not in body
    # The risk card and copilot arrive later into empty or hidden slots.
    slot = body.split('id="portfolio-risk"', 1)[1].split("</div>", 1)[0]
    assert slot.rstrip().endswith('hx-swap="innerHTML">')
    nav = body.split('class="portfolio-views"', 1)[1].split("</nav>", 1)[0]
    assert 'data-portfolio-view="holdings" aria-current="page"' in nav
    assert 'hx-push-url="/portfolio/trades"' in nav


def test_weight_flags_a_holding_over_the_position_limit(stack) -> None:
    body = _holdings(stack)
    row = body.split("openSell('AAA'", 1)[0].rsplit("<tr", 1)[1]

    # AAA is £1,000 of £2,000 (BBB is unpriced): 50% against a 20% limit.
    assert (
        '<span class="weight-over" title="Over the 20% single-position limit">50.0%'
        in row
    )
    cash = body.split('class="portfolio-cash-row"', 1)[1].split("</tr>", 1)[0]
    assert "50.0%" in cash


def test_only_the_unstopped_holding_gets_set_stop(stack) -> None:
    body = _holdings(stack)

    assert f'aria-controls="stop-slot-{stack.pid}-BBB"' in body
    assert f"stop-slot-{stack.pid}-AAA" not in body


def test_clear_lives_only_in_the_more_menu(stack) -> None:
    context = stack.service.default_portfolio_context(stack.pid)
    context["strategy_assignment"] = SimpleNamespace(
        available=True, display_name="Alpha"
    )
    html = templates.get_template("_portfolio.html").render(**context)

    menu = html.split('id="portfolio-more-menu"', 1)[1]
    before = html.split('id="portfolio-more-menu"', 1)[0]
    assert "/strategy/clear" in menu and "Change strategy" in menu
    assert "/strategy/clear" not in before
    assert "Strategy: Alpha" in before


def test_columns_empty_for_every_row_are_dropped() -> None:
    bare = Position.model_construct(entry_price=None, next_pivot=None, exit_signal=None)
    pivot = bare.model_copy(update={"entry_price": 10.0})

    assert holding_columns([bare]) == {
        "last_pivot": False,
        "next_pivot": False,
        "signal": False,
    }
    assert holding_columns([bare, pivot])["last_pivot"] is True


def test_foreign_cash_sits_in_the_cash_activity_disclosure(stack) -> None:
    context = stack.service.default_portfolio_context(stack.pid)
    usd = {
        "currency": "USD",
        "amount": 4.5,
        "projection": SimpleNamespace(status="unavailable", reason="no quote"),
    }
    context["cash_balances_by_currency"] = [usd]
    context["foreign_cash"] = [usd]
    html = templates.get_template("_portfolio.html").render(**context)

    disclosure = html.split('<details class="disclosure">', 1)[1]
    assert "Cash activity" in disclosure.split("</summary>", 1)[0]
    assert "also holds USD 4.50" in disclosure.split("</summary>", 1)[0]
    assert "GBP valuation unavailable" in disclosure
    assert html.count("USD 4.50") == 2  # the summary and the line inside


# --- AD-5: the Risk check card ------------------------------------------------


def test_risk_card_is_risk_items_once_each_and_matches_today(stack) -> None:
    body = client.get(
        "/partials/portfolio/risk", params={"portfolio_id": stack.pid}
    ).text
    risks = stack.desk().risk_items(stack.pid)
    today = [
        i
        for i in stack.desk().today_items(stack.pid).needs_action
        if i.kind == "held_risk"
    ]

    assert risks and [i.key for i in risks] == [i.key for i in today]
    lines = body.split('<details class="disclosure">', 1)[0]
    assert lines.count('class="portfolio-risk-item"') == len(risks)
    for item in risks:
        assert lines.count(f'data-item-key="{item.key}"') == 1
        assert lines.count(f">{item.title}</div>") == 1
    assert f"{len(risks)} issue" in lines


def test_risk_card_declares_an_unavailable_projection(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> SimpleNamespace:
        def fail(_pid: object) -> None:
            raise RuntimeError("boom")

        return SimpleNamespace(risk_items=fail)

    monkeypatch.setitem(app.dependency_overrides, get_desk_service, broken)
    resp = client.get("/partials/portfolio/risk", params={"portfolio_id": stack.pid})

    assert resp.status_code == 200
    assert "The risk check is unavailable right now." in resp.text
    assert "Policy and sector notes" in resp.text


def _finding(kind: str, severity: str, title: str, *tickers: str) -> RiskFindingV1:
    return RiskFindingV1.model_validate(
        {
            "kind": kind,
            "severity": severity,
            "title": title,
            "detail": "d",
            "tickers": tickers,
        }
    )


def test_risk_notes_keep_only_what_the_queue_does_not_take() -> None:
    report = RiskReportV1(
        policy=RiskPolicyV1(),
        findings=(
            _finding("position_concentration", "high", "AAA is 50%", "AAA"),
            _finding("no_stop", "medium", "BBB has no stop", "BBB"),
            _finding("unknown_sector", "info", "Unknown sector", "BBB"),
            _finding("cash", "info", "Cash"),
        ),
        confidence="limited",
        limitations=("1 holding(s) unpriced.",),
        total_value_gbp=Decimal("100"),
    )

    assert risk_notes(report) == (
        "1 holding(s) unpriced.",
        "Unknown sector (BBB): d",
    )


# --- The chart opens on the history that exists -------------------------------


def _service(earliest: str | None) -> PortfolioService:
    trader = MagicMock()
    trader.earliest_snapshot_timestamp.return_value = earliest
    return PortfolioService(trader)


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


@pytest.mark.parametrize(
    ("earliest", "requested", "expected"),
    [
        (_days_ago(10), "12M", "1M"),
        (_days_ago(60), "12M", "3M"),
        (_days_ago(60), "1M", "1M"),
        (_days_ago(800), "12M", "12M"),
        (None, "12M", "12M"),
    ],
)
def test_covering_range_is_the_narrowest_preset_holding_the_data(
    earliest: str | None, requested: str, expected: str
) -> None:
    assert _service(earliest).covering_range(1, requested) == expected


def test_covering_range_without_a_portfolio_keeps_the_request() -> None:
    assert _service(_days_ago(10)).covering_range(None, "12M") == "12M"
