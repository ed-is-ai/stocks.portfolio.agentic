"""Route tests for the History tab's process review (GH-17).

The service runs over a tmp ``trades.db`` (never the real one) with the
checklist tests' ``FakeReader`` for evidence. Covers the lazy Process
column, the review modal, annotate (ok and invalid), the weekly strip's
three parts, interpretation ok/unavailable, auth and the no-writes boundary.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.trade_review.checklist import RULES
from app.api.app import app
from app.api.dependencies import get_trade_review_client, get_trade_review_service
from app.api.routes.trade_review import process_cell
from app.api.templating import templates
from app.core.config import TEMPLATES_DIR as TEMPLATES
from app.schemas import Trade
from app.schemas.trade_review import (
    CheckKind,
    CheckStatus,
    TradeCheckV1,
    TradeReviewV1,
)
from tests.test_trade_review_checklist import FakeReader
from tests.test_trade_review_weekly import GOOD, INTENT, _client, build_stack

client = TestClient(app)
AUTH = {"X-Auth-Token": "s3cret"}
UNCHANGED_TABLES = (
    "trades",
    "cash_flows",
    "portfolios",
    "portfolio_strategies",
    "portfolio_strategy_history",
)


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    built = build_stack(tmp_path)
    built.client = _client(GOOD)[0]
    app.dependency_overrides[get_trade_review_service] = lambda: built.service
    app.dependency_overrides[get_trade_review_client] = lambda: built.client
    try:
        yield built
    finally:
        app.dependency_overrides.clear()


def _tables(stack: SimpleNamespace) -> list[list[tuple[Any, ...]]]:
    with closing(sqlite3.connect(stack.agent.db_path)) as conn:
        return [
            conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in UNCHANGED_TABLES
        ]


def _annotations(stack: SimpleNamespace) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(stack.agent.db_path)) as conn:
        return conn.execute(
            "SELECT trade_id, intent, stated_stop FROM trade_annotations ORDER BY id"
        ).fetchall()


def _no_issue_numbers(html: str) -> None:
    assert "GH-" not in html and "#17" not in html


def test_process_column_fills_each_row_out_of_band(stack) -> None:
    resp = client.get("/partials/history/process")

    assert resp.status_code == 200
    for trade in (stack.first, stack.second):
        assert f'id="process-{trade.id}"' in resp.text
    assert resp.text.count('hx-swap-oob="true"') == 2
    assert "1 deviated" in resp.text
    _no_issue_numbers(resp.text)


def test_history_renders_placeholders_and_the_lazy_loaders() -> None:
    trade = Trade(
        id=7,
        ticker="AAPL",
        action="BUY",
        shares=1,
        price=1.0,
        date="2026-01-01",
        portfolio_id=1,
    )
    html = templates.get_template("_history.html").render(
        trades=[trade], portfolio_names={1: "SIPP"}, opening_lot_status={}
    )

    assert "<th>Process</th>" in html
    assert 'id="process-7"' in html and "data-process-placeholder" in html
    assert 'hx-get="/partials/history/process"' in html
    assert 'hx-get="/partials/history/weekly"' in html
    assert "tradeProcessUnavailable(event)" in html


def test_review_modal_shows_checks_evidence_and_the_form(stack) -> None:
    resp = client.get(f"/partials/history/process/{stack.first.id}")

    assert resp.status_code == 200
    assert "Entry location" in resp.text and "entry.pivot_band" in resp.text
    assert "extended" in resp.text
    assert "committed monthly scan · session" in resp.text
    assert f'hx-post="/trades/{stack.first.id}/annotations"' in resp.text
    _no_issue_numbers(resp.text)


def test_annotate_appends_a_row_and_refreshes_the_review(stack) -> None:
    before = _tables(stack)

    resp = client.post(
        f"/trades/{stack.first.id}/annotations",
        data={"intent": INTENT, "stated_stop": "117.28"},
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "trade-process-refresh"
    assert "Annotation saved." in resp.text
    assert "your annotation" in resp.text
    assert _annotations(stack) == [(stack.first.id, INTENT, 117.28)]
    assert _tables(stack) == before


@pytest.mark.parametrize(
    ("data", "warning"),
    [
        ({"intent": "x" * 1001, "stated_stop": ""}, "at most 1000"),
        ({"intent": "why", "stated_stop": "abc"}, "must be a number"),
        ({"intent": "why", "stated_stop": "-5"}, "positive price"),
        ({"intent": "  ", "stated_stop": ""}, "Add an intent"),
    ],
    ids=["too-long", "not-a-number", "negative", "empty"],
)
def test_invalid_annotation_warns_and_writes_nothing(
    stack, data: dict[str, str], warning: str
) -> None:
    resp = client.post(f"/trades/{stack.first.id}/annotations", data=data, headers=AUTH)

    assert resp.status_code == 200
    assert warning in resp.text
    assert "HX-Trigger" not in resp.headers
    assert _annotations(stack) == []


def test_annotating_an_unknown_trade_is_404(stack) -> None:
    resp = client.post("/trades/999/annotations", data={"intent": "x"}, headers=AUTH)

    assert resp.status_code == 404
    assert _annotations(stack) == []


def test_posts_require_auth(stack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    cross = {"Sec-Fetch-Site": "cross-site"}

    annotate = client.post(
        f"/trades/{stack.first.id}/annotations", data={"intent": "x"}, headers=cross
    )
    interpret = client.post("/partials/history/weekly/interpretation", headers=cross)

    assert annotate.status_code == 403
    assert interpret.status_code == 403
    assert _annotations(stack) == []


def test_weekly_strip_shows_three_separate_parts(stack) -> None:
    stack.annotations.add(stack.pid, stack.first.id, INTENT, None)

    resp = client.get("/partials/history/weekly")

    assert resp.status_code == 200
    for heading in (">Facts<", ">Your annotations<", ">Model interpretation<"):
        assert heading in resp.text
    assert INTENT in resp.text
    assert "entry.pivot_band" in resp.text and "Recurring deviations" in resp.text
    assert "Interpret with AI" in resp.text
    _no_issue_numbers(resp.text)


def test_weekly_navigation_moves_between_weeks_with_trades(stack) -> None:
    stack.agent.record_buy("ZETA", 1, 99.0, "2024-06-03", portfolio_id=stack.pid)

    latest = client.get("/partials/history/weekly").text
    earlier = client.get("/partials/history/weekly?week=2024-W23").text

    assert 'hx-get="/partials/history/weekly?week=2024-W23"' in latest
    assert 'hx-get="/partials/history/weekly?week=2025-W01"' in earlier


def test_interpretation_ok_and_unavailable(stack) -> None:
    ok = client.post("/partials/history/weekly/interpretation", headers=AUTH)
    stack.client = _client(GOOD, stop_reason="refusal")[0]
    unavailable = client.post("/partials/history/weekly/interpretation", headers=AUTH)

    assert ok.status_code == 200 and GOOD["summary"] in ok.text
    assert "Model interpretation" in ok.text
    assert unavailable.status_code == 200
    assert "Interpretation unavailable" in unavailable.text


def test_store_unavailable_still_renders_the_column(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    built = build_stack(tmp_path, FakeReader(security=None, failed=True))
    app.dependency_overrides[get_trade_review_service] = lambda: built.service
    try:
        resp = client.get("/partials/history/process")
        modal = client.get(f"/partials/history/process/{built.first.id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200 and "Evidence limited" in resp.text
    assert "Unknown" in modal.text


def test_process_cell_ignores_an_unreplayed_strategy() -> None:
    rule = RULES["strategy_alignment"]

    def review(*checks: tuple[CheckKind, CheckStatus]) -> TradeReviewV1:
        return TradeReviewV1(
            trade_id=1,
            portfolio_id=1,
            ticker="ZETA",
            action="BUY",
            trade_date=date(2025, 1, 2),
            checks=tuple(
                TradeCheckV1(kind=k, status=s, rule=rule, calculation="")
                for k, s in checks
            ),
        )

    clean = review(("valid_setup", "followed"), ("strategy_alignment", "unknown"))
    gap = review(("valid_setup", "unknown"), ("strategy_alignment", "n_a"))

    assert process_cell(clean).text == "Followed"
    assert process_cell(gap).text == "Evidence limited"


def test_a_stop_stated_after_the_trade_is_not_called_prior_evidence(stack) -> None:
    client.post(
        f"/trades/{stack.first.id}/annotations",
        data={"intent": INTENT, "stated_stop": "117.28"},
        headers=AUTH,
    )
    stated = stack.annotations.all()[0].created_at[:10]

    html = client.get(f"/partials/history/process/{stack.first.id}").text

    assert f"stated by you on {stated}, after the trade" in html
    assert "evidence read before the trade date, except a stop you stated after it" in (
        html
    )


def test_weekly_and_modal_fall_back_when_the_review_fails(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("broken")

    monkeypatch.setattr(stack.service, "weekly", boom)
    monkeypatch.setattr(stack.service, "review", boom)

    weekly = client.get("/partials/history/weekly")
    modal = client.get(f"/partials/history/process/{stack.first.id}")

    assert weekly.status_code == 200 and "Review unavailable" in weekly.text
    assert modal.status_code == 200 and "Review unavailable" in modal.text


def test_annotating_recomputes_only_that_portfolio(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = stack.agent.create_portfolio("ISA")
    stack.agent.record_buy("ZETA", 1, 99.0, "2024-06-03", portfolio_id=other.id)
    client.get("/partials/history/process")  # warm the cache
    realised = stack.service._realised
    real = realised.fifo_lots
    calls: list[int] = []

    def spy(portfolio_id: int) -> Any:
        calls.append(portfolio_id)
        return real(portfolio_id)

    monkeypatch.setattr(realised, "fifo_lots", spy)

    resp = client.post(
        f"/trades/{stack.first.id}/annotations", data={"intent": "x"}, headers=AUTH
    )

    assert resp.status_code == 200 and "Annotation saved." in resp.text
    assert calls == [stack.pid]


def test_interpreting_a_week_without_trades_says_so(stack) -> None:
    stack.client, calls = _client(GOOD)

    resp = client.post(
        "/partials/history/weekly/interpretation?week=2020-W01", headers=AUTH
    )

    assert resp.status_code == 200
    assert "That week has no trades." in resp.text
    assert calls == []


def test_review_modal_errors_are_visible() -> None:
    index = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    target = index.split('<div id="trade-review-modal-target"', 1)[1].split(">")[0]
    cell = (TEMPLATES / "_trade_process.html").read_text(encoding="utf-8")
    button = cell.split("<button", 1)[1].split(">", 1)[0]

    assert 'hx-on::before-swap="thesisSwapNotFound(event)"' in target
    for tag in (target, button):
        assert 'hx-on::response-error="tradeReviewRequestFailed(event)"' in tag
        assert 'hx-on::send-error="tradeReviewRequestFailed(event)"' in tag
    assert "function tradeReviewRequestFailed(event)" in index
    assert "event.target.closest('#trade-review-body')" in index
    template = index.split('<template id="trade-review-error-modal">', 1)[1]
    template = template.split("</template>", 1)[0]
    assert 'id="trade-review-body"' in template
    failed = index.split("const TRADE_REVIEW_REQUEST_FAILED =", 1)[1].split(";")[0]
    assert "request failed" in failed
    assert not re.search(r"#1[0-9]\b|GH-", failed)


def test_an_unknown_trade_404_is_swappable_html(stack) -> None:
    resp = client.post("/trades/999/annotations", data={"intent": "x"}, headers=AUTH)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert 'id="trade-review-body"' in resp.text
