"""Real-stack tests for the Portfolio tab's lazy agent layer (GH-19)."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents.trader.trader_agent import TraderAgent
from app.api.app import app
from app.api.dependencies import (
    get_portfolio_recommendation_service,
    get_portfolio_service,
    get_position_thesis_service,
    get_trader_service,
)
from app.repositories import db
from app.repositories.position_theses_repo import PositionThesesRepository
from app.schemas.position_thesis import ThesisContentV1
from app.schemas.portfolio_recommendation import (
    NO_ASSIGNMENT,
    EvaluationCoverageV1,
    RecommendationEvidenceDiagnosticV1,
    RecommendationResultV1,
    RecommendationV1,
)
from app.services.portfolio_agent_view import RecommendationOutcome
from app.services.portfolio_service import PortfolioService
from app.services.position_thesis_service import PositionThesisService
from app.services.trader_service import TraderService
from tests.test_portfolio_risk_route import _dump, _record

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
    theses = PositionThesisService(
        PositionThesesRepository(db.make_connect(lambda: agent.db_path)),
        trader,
        tmp_path / "no-artifact.json",
    )
    app.dependency_overrides[get_trader_service] = lambda: trader
    app.dependency_overrides[get_portfolio_service] = lambda: service
    app.dependency_overrides[get_portfolio_recommendation_service] = lambda: (
        recommendations
    )
    app.dependency_overrides[get_position_thesis_service] = lambda: theses
    try:
        yield SimpleNamespace(
            agent=agent, pid=pf.id, service=service, outcome=outcome, theses=theses
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


def test_thesis_editor_modal_target_sits_outside_the_tab() -> None:
    index = (ROOT / "app/api/templates/index.html").read_text(encoding="utf-8")

    assert index.index('id="tab-content"') < index.index(
        'id="thesis-editor-modal-target"'
    )


def test_agents_partial_performs_no_writes(stack) -> None:
    before = _dump(stack.agent.db_path)
    for params in ({"portfolio_id": stack.pid}, {"portfolio_id": ""}, {}):
        assert (
            client.get("/partials/portfolio/agents", params=params).status_code == 200
        )
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
    assert "!(event && event.detail && event.detail.successful)" in index
    assert '[data-agent-cell="${scope}"]' in index
