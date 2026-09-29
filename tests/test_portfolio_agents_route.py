"""Real-stack tests for the Portfolio tab's lazy agent layer (GH-19)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents.thesis.evaluator import evaluate_thesis
from app.agents.trader.trader_agent import TraderAgent
from app.api.app import app
from app.api.dependencies import (
    get_notifications_repository,
    get_portfolio_recommendation_service,
    get_portfolio_service,
    get_position_thesis_service,
    get_trader_service,
)
from app.repositories import db
from app.repositories.notifications_repo import NotificationsRepository
from app.repositories.position_theses_repo import PositionThesesRepository
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.position_thesis import ThesisContentV1
from app.schemas.record import StockRecord
from app.schemas.portfolio_recommendation import (
    NO_ASSIGNMENT,
    EvaluationCoverageV1,
    RecommendationEvidenceDiagnosticV1,
    RecommendationResultV1,
    RecommendationV1,
)
from app.agents.triage.sources import setup_events
from app.api.routes import views as views_module
from app.api.templating import templates
from app.schemas.source_health import SourceHealth, SourceName, SourceState
from app.services import portfolio_service as portfolio_service_module
from app.services.portfolio_agent_view import (
    PortfolioAgentView,
    PublishedEvidence,
    RecommendationOutcome,
    build_agent_view,
)
from app.services.portfolio_service import PortfolioService
from app.services.position_thesis_service import PositionThesisService
from app.services.trader_service import TraderService
from tests.test_alert_digest_held import _buy_record
from tests.test_portfolio_agent_view import _risk
from tests.test_portfolio_risk_route import _dump, _record
from tests.test_thesis_evaluator import FRESH, META, make_record

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


def _rec(action: str, ticker: str) -> RecommendationV1:
    return RecommendationV1.model_validate(
        {
            "action": action,
            "ticker": ticker,
            "security_id": ticker,
            "rule_id": "r",
            "reason": f"{action} rule",
        }
    )


def _result() -> RecommendationResultV1:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    short = RecommendationEvidenceDiagnosticV1.model_validate(
        {
            "security_id": "BBB",
            "display_ticker": "BBB",
            "path": "exit",
            "required_sessions": 200,
            "available_sessions": 166,
            "cause": "short_history",
            "disposition": "hold",
        }
    )
    return RecommendationResultV1(
        portfolio_id=1,
        analysis_run_id="run-1",
        generated_at=now,
        market_session=date(2026, 9, 25),
        freshness="fresh",
        strategy_id="alpha",
        strategy_source_digest="a" * 64,
        parameters={},
        recommendations=(_rec("sell", "AAA"), _rec("hold", "BBB")),
        coverage=EvaluationCoverageV1(diagnostics=(short,)),
        evaluated_at=now,
    )


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The Risk Coach stack (AAA 50% with a stop, BBB unpriced) plus a fake
    recommendation service whose outcome each test sets."""
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAA", 10, 90.0, "2026-01-02", stop_loss=85.0, portfolio_id=pf.id)
    agent.record_buy("BBB", 5, 10.0, "2026-01-02", portfolio_id=pf.id)
    agent.set_cash_balance(1000.0, pf.id)
    agent.save_price_cache({"AAA": 100.0}, {"AAA": (100.0, "GBP")})
    trader = TraderService(agent)
    service = PortfolioService(trader)
    monkeypatch.setattr(
        service, "load_analysis", lambda: [_record("AAA", 100.0, 80.0, "Energy")]
    )
    outcome: dict[str, Callable[[int], RecommendationOutcome]] = {
        "recommend": lambda _pid: _result()
    }
    recommendations = SimpleNamespace(recommend=lambda pid: outcome["recommend"](pid))
    artifact = tmp_path / "no-artifact.json"
    # The attention queue reads the published artifact's freshness and the
    # run's source health; keep both isolated and fresh by default.
    monkeypatch.setattr(portfolio_service_module, "ANALYSIS_JSON", artifact)
    monkeypatch.setattr(views_module, "load_source_health", dict)
    artifact.write_text(
        json.dumps(
            build_analysis_payload([], run_id="run-0", generated_at=datetime.now(UTC))
        ),
        encoding="utf-8",
    )
    theses = PositionThesisService(
        PositionThesesRepository(db.make_connect(lambda: agent.db_path)),
        trader,
        artifact,
    )
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: service
    app.dependency_overrides[get_portfolio_recommendation_service] = lambda: (
        recommendations
    )
    app.dependency_overrides[get_position_thesis_service] = lambda: theses
    notifications = NotificationsRepository(
        db.make_connect(lambda: tmp_path / "notifications.db")
    )
    notifications.ensure_schema()
    app.dependency_overrides[get_notifications_repository] = lambda: notifications
    try:
        yield SimpleNamespace(
            agent=agent,
            notifications=notifications,
            pid=pf.id,
            service=service,
            outcome=outcome,
            theses=theses,
            artifact=artifact,
        )
    finally:
        app.dependency_overrides.clear()


def _raise(*_args: object) -> RecommendationOutcome:
    raise RuntimeError("boom")


def _agents(stack: SimpleNamespace) -> str:
    resp = client.get("/partials/portfolio/agents", params={"portfolio_id": stack.pid})
    assert resp.status_code == 200
    return resp.text


def _oob(body: str, element_id: str) -> str:
    """Return the text of the out-of-band element ``element_id``."""
    pattern = rf'id="{element_id}"[^>]*hx-swap-oob="true"[^>]*>(.*?)</(?:span|div)>\n'
    match = re.search(pattern, body, re.S)
    assert match is not None, element_id
    return re.sub(r"<[^>]+>", " ", match.group(1))


def _cell(stack: SimpleNamespace, body: str, name: str) -> str:
    """Return the text of this portfolio's out-of-band element ``name``."""
    return _oob(body, f"agent-{stack.pid}-{name}")


def test_all_live_sources_swap_into_the_rows_cards_and_strip(stack) -> None:
    body = _agents(stack)

    assert "Sell" in _cell(stack, body, "strategy-AAA")
    assert "Hold" in _cell(stack, body, "strategy-BBB")
    assert "50.0%" in _cell(stack, body, "risk-AAA")
    # The row is flagged by its own high concentration finding.
    assert 'class="status risk">50.0%' in body
    assert "166 / 200" in _cell(stack, body, "evidence-BBB")
    assert "Complete" in _cell(stack, body, "evidence-AAA")
    assert "£150.00 · 7.5%" in _cell(stack, body, "open-risk")
    # 3 high risk findings (position, sector, capital at risk) + 1 Sell +
    # 1 exit evidence gap.
    assert _cell(stack, body, "findings").strip() == "5"
    assert "5 urgent findings." in body
    assert "partial" not in body


def test_no_strategy_is_declared_and_count_is_partial(stack) -> None:
    stack.outcome["recommend"] = lambda _pid: NO_ASSIGNMENT
    body = _agents(stack)

    assert "No Strategy assigned" in _cell(stack, body, "strategy-AAA")
    assert "No Strategy assigned" in _cell(stack, body, "evidence-AAA")
    assert "3 urgent findings." in body
    assert _cell(stack, body, "findings").strip() == "3 · partial"


def test_zero_urgent_findings_with_a_gap_is_not_shown_clear(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack.outcome["recommend"] = lambda _pid: NO_ASSIGNMENT
    monkeypatch.setattr(stack.service, "risk_report", _raise)
    body = _agents(stack)

    assert "No urgent findings." in body
    assert _cell(stack, body, "findings").strip() == "0 · partial"
    assert "is-clear" not in body


def test_recommendation_failure_is_caught(stack) -> None:
    stack.outcome["recommend"] = _raise
    cell = _cell(stack, _agents(stack), "strategy-AAA")

    assert "Strategy unavailable" in cell
    assert "Recommendations could not be evaluated" in cell


def test_risk_failure_is_caught(stack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack.service, "risk_report", _raise)
    body = _agents(stack)

    assert "Risk unavailable" in _cell(stack, body, "risk-AAA")
    assert "Unavailable" in _cell(stack, body, "open-risk")
    assert "not counted: Risk unavailable." in body


def test_no_portfolios_survives_a_failing_risk_report(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack.service._trader, "list_portfolios", lambda: [])
    monkeypatch.setattr(stack.service, "risk_report", _raise)

    view = stack.service.agent_view(None, _raise, _raise)

    assert view.portfolio_id is None
    assert view.unavailable == ("Risk unavailable",)


def test_thesis_cells_swap_in_and_count_nothing_by_default(stack) -> None:
    content = ThesisContentV1.model_validate(
        {
            "rationale": "r",
            "expected_setup": "s",
            "rules": [{"kind": "stage_2_lost"}],
        }
    )
    PositionThesesRepository(db.make_connect(lambda: stack.agent.db_path)).add_version(
        stack.pid, "AAA", content, "user", active=True
    )
    body = _agents(stack)

    assert "Awaiting scan" in _cell(stack, body, "thesis-AAA")
    assert "No thesis" in _cell(stack, body, "thesis-BBB")
    assert _cell(stack, body, "findings").strip() == "5"


def test_thesis_store_failure_is_caught_and_partial(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack.theses, "statuses", _raise)
    body = _agents(stack)

    assert "Thesis unavailable" in _cell(stack, body, "thesis-AAA")
    assert "Thesis unavailable" in _cell(stack, body, "thesis-BBB")
    assert _cell(stack, body, "findings").strip() == "5 · partial"
    assert "not counted: Thesis unavailable." in body


class _Ancestors(HTMLParser):
    """Record the element ids enclosing every element with id ``target``."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link"}
    VOID |= {"meta", "source", "track", "wbr"}

    def __init__(self, target: str) -> None:
        super().__init__()
        self.target = target
        self.open: list[tuple[str, str | None]] = []
        self.ancestors: list[str | None] = []
        self.seen = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element_id = dict(attrs).get("id")
        if element_id == self.target:
            self.seen = True
            self.ancestors += [open_id for _tag, open_id in self.open]
        if tag not in self.VOID:
            self.open.append((tag, element_id))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.open) - 1, -1, -1):
            if self.open[depth][0] == tag:
                del self.open[depth:]
                return


def _ancestor_ids(html: str, target: str) -> list[str | None]:
    parser = _Ancestors(target)
    parser.feed(html)
    assert parser.seen, target
    return parser.ancestors


def test_thesis_editor_modal_target_sits_outside_the_tab() -> None:
    index = (ROOT / "app/api/templates/index.html").read_text(encoding="utf-8")
    target = "thesis-editor-modal-target"

    assert "tab-content" not in _ancestor_ids(index, target)
    # The walk does see nesting: the same target inside the tab is caught.
    nested = f'<div id="tab-content"><p><br><div id="{target}"></div></p></div>'
    assert "tab-content" in _ancestor_ids(nested, target)


def _publish(stack: SimpleNamespace, run_id: str) -> None:
    payload = build_analysis_payload([], run_id=run_id, generated_at=datetime.now(UTC))
    stack.artifact.write_text(json.dumps(payload), encoding="utf-8")


def _seed_thesis(
    stack: SimpleNamespace, rule: dict[str, object], record: StockRecord
) -> None:
    """Store an active AAA thesis evaluated against the published run-1."""
    repo = PositionThesesRepository(db.make_connect(lambda: stack.agent.db_path))
    content = ThesisContentV1.model_validate(
        {"rationale": "r", "expected_setup": "s", "rules": [rule]}
    )
    thesis = repo.add_version(stack.pid, "AAA", content, "user", active=True)
    assert repo.append_evaluation(evaluate_thesis(thesis, record, META, FRESH))
    _publish(stack, META.run_id)


def _thesis_status(stack: SimpleNamespace, body: str) -> tuple[str, str]:
    """Return the AAA Thesis cell's (tone, status text)."""
    match = re.search(
        rf'id="agent-{stack.pid}-thesis-AAA"[^>]*>\s*'
        r'<span class="status (\w+)">([^<]+)</span>',
        body,
    )
    assert match is not None
    return match.group(1), match.group(2)


def test_current_invalidated_thesis_is_an_urgent_finding(stack) -> None:
    _seed_thesis(
        stack, {"kind": "close_below_sma", "period": 50}, make_record(price=94.0)
    )
    body = _agents(stack)

    assert _thesis_status(stack, body) == ("risk", "Invalidated")
    # It joins AAA's Sell as one exit item, both events expandable.
    assert _cell(stack, body, "findings").strip() == "5"
    assert "5 urgent findings." in body
    assert "AAA: thesis invalidated" in body
    assert "2 source events" in body


@pytest.mark.parametrize(
    ("record", "status"),
    [
        (make_record(price=91.8, stop=90.0), "Weakened"),
        (make_record(stop=None), "Evidence limited"),
    ],
    ids=["weakened", "evidence-limited"],
)
def test_current_warning_thesis_is_warn_toned_and_not_counted(
    stack, record: StockRecord, status: str
) -> None:
    _seed_thesis(stack, {"kind": "close_below_stop"}, record)
    body = _agents(stack)

    assert _thesis_status(stack, body) == ("warn", status)
    assert "earlier run" not in _cell(stack, body, "thesis-AAA")
    assert _cell(stack, body, "findings").strip() == "5"
    assert "5 urgent findings." in body
    assert "AAA: thesis" not in body


def test_agents_partial_performs_no_writes(stack) -> None:
    before = _dump(stack.agent.db_path)
    for params in ({"portfolio_id": stack.pid}, {"portfolio_id": ""}, {}):
        assert (
            client.get("/partials/portfolio/agents", params=params).status_code == 200
        )
    assert _dump(stack.agent.db_path) == before


def test_agents_partial_never_evaluates_a_newly_published_run(stack) -> None:
    _seed_thesis(stack, {"kind": "stage_2_lost"}, make_record())
    _publish(stack, "run-2")
    before = _dump(stack.agent.db_path)
    assert any("thesis_evaluations" in line for line in before)

    body = _agents(stack)

    assert "last checked on an earlier run" in _cell(stack, body, "thesis-AAA")
    assert _dump(stack.agent.db_path) == before


def test_tab_renders_scoped_placeholders_and_one_lazy_loader(stack) -> None:
    resp = client.get("/partials/portfolio", params={"portfolio_id": stack.pid})
    html = resp.text
    pid = stack.pid

    assert resp.status_code == 200
    assert html.count("/partials/portfolio/agents?") == 1
    assert f'hx-get="/partials/portfolio/agents?portfolio_id={pid}"' in html
    assert 'hx-sync="this:replace"' in html
    assert (
        f"hx-on::after-request=\"portfolioAgentsUnavailable('{pid}', event)\"" in html
    )
    # Every id and placeholder is scoped to this portfolio, so a late
    # response for another portfolio cannot match (or blank) them.
    for name in (
        "strategy-AAA",
        "risk-BBB",
        "evidence-BBB",
        "thesis-BBB",
        "open-risk",
        "findings",
        "attention",
    ):
        assert f'id="agent-{pid}-{name}"' in html
    assert html.count(f'data-agent-placeholder="{pid}"') == 4 * 2 + 3
    assert "data-agent-placeholder>" not in html
    assert "Thesis monitor not available yet" not in html
    # A static Thesis button per row opens the editor outside #tab-content,
    # and a thesis write reloads the agent layer.
    assert f'hx-get="/portfolios/{pid}/theses/AAA"' in html
    assert html.count('hx-target="#thesis-editor-modal-target"') == 2
    assert 'hx-trigger="load, portfolio-agents-refresh from:body"' in html
    assert not re.search(r"\(#1[34]\)", html)
    # The aside: context, declared portfolio-level gap, target, boundary.
    assert 'id="portfolio-copilot-body"' in html
    assert (
        "Portfolio-level answers are not available: the Research copilot "
        "explains one security at a time. Use Why? on a holding."
    ) in html
    assert (
        "<strong>Read-only.</strong> The model explains and proposes. Typed "
        "services calculate; state-changing jobs require approval."
    ) in html
    # The new cards are appended after Cash, in the existing grid.
    grid = html.split('class="portfolio-summary-grid"', 1)[1].split("</section>")[0]
    assert grid.index(">Cash</div>") < grid.index(">Open risk</div>")
    assert grid.index(">Open risk</div>") < grid.index(">Agent findings</div>")


def test_partial_ids_match_the_tab_placeholders(stack) -> None:
    tab = client.get("/partials/portfolio", params={"portfolio_id": stack.pid}).text
    swapped = re.findall(r'id="([^"]+)"[^>]*hx-swap-oob="true"', _agents(stack))

    assert swapped and all(f'id="{i}"' in tab for i in swapped)


def test_row_why_posts_to_the_aside_without_the_offcanvas(stack) -> None:
    html = client.get("/partials/portfolio", params={"portfolio_id": stack.pid}).text
    row = html.split(f'id="agent-{stack.pid}-strategy-AAA"', 1)[1]
    row = row.split("</td>", 1)[0]

    assert 'hx-post="/copilot/ask"' in row
    assert 'hx-vals=\'{"target": "#portfolio-copilot-body", "ticker": "AAA"}\'' in row
    assert 'hx-target="#portfolio-copilot-body"' in row
    assert 'hx-on::after-request="revealPortfolioCopilot()"' in row
    assert "offcanvas" not in row


def test_failure_fallback_and_chart_include_are_in_place() -> None:
    index = (ROOT / "app/api/templates/index.html").read_text(encoding="utf-8")
    template = (ROOT / "app/api/templates/_portfolio.html").read_text(encoding="utf-8")

    assert "function portfolioAgentsUnavailable(portfolioId, event)" in index
    assert '[data-agent-placeholder="${scope}"]' in index
    assert "'Agent data unavailable'" in index
    # copilotStatus tolerates a missing target; the aside scrolls into view
    # only once it stacks below the holdings.
    assert "if (!body) return;" in index
    assert "window.matchMedia('(max-width: 1080px)').matches" in index
    assert "grid-template-columns: minmax(0, 1fr) 340px" in index
    include = (
        '{% if chart_has_history %}{% include "_portfolio_chart.html" %}{% endif %}'
    )
    assert template.count(include) == 1
    # The aside sits beside the holdings only, below the chart and cards.
    assert (
        template.index(include)
        < template.index('class="portfolio-summary-grid"')
        < template.index('class="portfolio-holdings-layout"')
    )


def test_every_swapped_element_is_marked_for_a_failed_refresh(stack) -> None:
    body = _agents(stack)
    index = (ROOT / "app/api/templates/index.html").read_text(encoding="utf-8")

    swapped = re.findall(r"<[^>]*hx-swap-oob=\"true\"[^>]*>", body)
    assert swapped
    assert all(f'data-agent-cell="{stack.pid}"' in tag for tag in swapped)
    # A failed request marks filled cells too; a success only leftovers.
    assert "if (detail.successful) {" in index
    assert '[data-agent-cell="${scope}"]' in index


def test_a_superseded_agents_request_is_not_marked_failed() -> None:
    """hx-sync replace aborts the in-flight load; htmx fires after-request
    then send-abort from xhr.onabort, so failures are marked in a microtask
    only when no send-abort was recorded for that request's xhr."""
    index = (ROOT / "app/api/templates/index.html").read_text(encoding="utf-8")
    handler = index.split("function portfolioAgentsUnavailable(", 1)[1]
    handler = handler.split("\n  }\n", 1)[0]

    assert "document.body.addEventListener('htmx:sendAbort'" in index
    assert "abortedRequests.add(event.detail.xhr);" in index
    failed = handler.split("queueMicrotask(() => {", 1)[1]
    assert failed.index("abortedRequests.has(detail.xhr)) return;") < failed.index(
        '[data-agent-cell="${scope}"]'
    )


# ── Published evidence and the strip (GH-18) ──────────────────────────────


def _view_with(stack: SimpleNamespace, **health: object):
    return stack.service.agent_view(
        stack.pid, lambda _pid: _result(), stack.theses.statuses, lambda: health
    )


def _publish_records(stack: SimpleNamespace, *records: StockRecord) -> datetime:
    at = datetime(2026, 9, 28, 21, tzinfo=UTC)
    rows = [r.model_dump(mode="json") for r in records]
    payload = build_analysis_payload(rows, run_id="run-7", generated_at=at)
    stack.artifact.write_text(json.dumps(payload), encoding="utf-8")
    return at


def test_no_published_artifact_is_unavailable_not_unknown_freshness(stack) -> None:
    stack.artifact.unlink()

    view = _view_with(stack)

    assert "Published analysis" in view.unavailable
    assert not any(e.kind.startswith("analysis_") for e in view.attention.events)
    assert "not counted: Published analysis." in _agents(stack)


def test_a_legacy_artifact_keeps_the_unknown_freshness_item(stack) -> None:
    stack.artifact.write_text(
        json.dumps([_buy_record("NEW", breakout=True).model_dump(mode="json")]),
        encoding="utf-8",
    )

    titles = [i.title for i in _view_with(stack).attention.items]

    assert "Analysis freshness is unknown" in titles
    assert "Stale: NEW: VCP Breakout" in titles


def test_setups_skip_securities_held_in_any_portfolio(stack) -> None:
    other = stack.agent.create_portfolio("ISA")
    stack.agent.record_buy("OTH", 1, 10.0, "2026-01-02", portfolio_id=other.id)
    _publish_records(
        stack, _buy_record("NEW", breakout=True), _buy_record("OTH", breakout=True)
    )

    setups = [i.title for i in _view_with(stack).attention.items]

    assert "NEW: VCP Breakout" in setups
    assert "OTH: VCP Breakout" not in setups


def test_published_evidence_comes_from_one_artifact_read(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _publish_records(stack, _buy_record("NEW", breakout=True))
    reads: list[Path] = []
    real = portfolio_service_module.read_analysis_snapshot

    def _spy(path: Path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(portfolio_service_module, "read_analysis_snapshot", _spy)

    view = _view_with(stack)

    assert reads == [stack.artifact]
    (setup,) = [e for e in view.attention.events if e.category == "new_setup"]
    assert setup.source_event_id == "setup:run-7:NEW:breakout"
    assert setup.observed_at == at
    assert view.attention.analysis_run_id == "run-7"


def test_no_portfolios_still_queues_published_evidence(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack.service._trader, "list_portfolios", lambda: [])
    failed = SourceHealth(source=SourceName.CONGRESS, state=SourceState.FAILED)

    view = stack.service.agent_view(
        None, _raise, _raise, lambda: {SourceName.CONGRESS: failed}
    )

    assert [i.title for i in view.attention.items] == ["Congress source failed"]
    assert view.attention.urgent_count == 0


def test_a_failing_published_evidence_path_is_fail_soft(
    stack, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _publish_records(stack, _buy_record("NEW", breakout=True))
    monkeypatch.setattr(portfolio_service_module, "record_setups", _raise)

    view = _view_with(stack)
    body = _agents(stack)

    assert "Published evidence" in view.unavailable
    assert not any(
        e.raised_by in ("scanner", "pipeline") for e in view.attention.events
    )
    assert "Published evidence failed" in caplog.text
    assert "Sell" in _cell(stack, body, "strategy-AAA")
    assert "166 / 200" in _cell(stack, body, "evidence-BBB")
    assert "not counted: Published evidence." in body


def _strip(view: PortfolioAgentView) -> str:
    return templates.get_template("_portfolio_agents.html").render(view=view)


def test_info_items_without_urgent_ones_are_introduced_not_shown_clear() -> None:
    setup = setup_events(
        [("NEW", "breakout", "VCP Breakout")], run_id="run-1", observed_at=None
    )
    published = PublishedEvidence(run_id="run-1", events=tuple(setup))
    risk = _risk()

    listed = _strip(build_agent_view(1, (), NO_ASSIGNMENT, risk, {}, published))
    empty = _strip(build_agent_view(1, (), NO_ASSIGNMENT, risk, {}))

    assert "No urgent findings." in listed
    assert listed.index("Also noted — not urgent") < listed.index("NEW: VCP Breakout")
    assert "is-clear" not in listed
    assert "is-clear" in empty
    assert "Also noted" not in empty


def test_the_portfolio_tab_queues_the_desks_notification_events(stack) -> None:
    """GH-21: the Portfolio tab and the AI Desk build one queue."""
    stack.notifications.record(
        NotificationCategory.PORTFOLIO,
        "import_failed",
        "Import rejected",
        severity=NotificationSeverity.ERROR,
        portfolio_id=stack.pid,
    )

    body = _agents(stack)

    assert "Import rejected" in body
    # Listed for review, never counted urgent.
    assert _cell(stack, body, "findings").strip() == "5"
