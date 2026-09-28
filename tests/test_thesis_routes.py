"""Real-stack tests for the position-thesis routes and editor (GH-14).

A tmp ``trades.db`` holds one portfolio with AAA open; a tmp analysis
artifact publishes AAA below its 50-day SMA. Covers save/draft/confirm, the
invalid-save, not-held and stale-draft rows, auth and the no-trade-writes
boundary.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.research.evidence import LABEL
from app.agents.thesis.drafter import ThesisDraftClient
from app.agents.trader.trader_agent import TraderAgent
from app.api.app import app
from app.api.dependencies import get_position_thesis_service, get_thesis_draft_client
from app.repositories import db
from app.repositories.position_theses_repo import PositionThesesRepository
from app.schemas.analysis_artifact import build_analysis_payload
from app.services.portfolio_agent_view import AgentCell, thesis_cell
from app.services.position_thesis_service import PositionThesisService
from app.services.trader_service import TraderService
from tests.test_thesis_evaluator import make_record

client = TestClient(app)
AUTH = {"X-Auth-Token": "s3cret"}
LEDGER_TABLES = ("trades", "cash_flows", "portfolios")
DRAFT = {
    "rationale": f"{LABEL} leads its group.",
    "expected_setup": f"{LABEL} holds its 50-day SMA.",
    "rules": [
        {"kind": "close_below_sma", "period": 50},
        {"kind": "score_below", "min_score": 5},
        {"kind": "close_below_sma", "period": 20},
    ],
}


def _fake_client(payload: Any) -> ThesisDraftClient:
    def create(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        )

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    return ThesisDraftClient(api_key="test-key", client=fake)


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr("app.api.routes.theses.load_source_health", lambda: {})
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAA", 10, 90.0, "2026-01-02", portfolio_id=pf.id)
    artifact = tmp_path / "analysis.json"
    payload = build_analysis_payload(
        [make_record(price=94.0).model_dump(mode="json")],
        run_id="run-1",
        generated_at=datetime.now(UTC),
    )
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    repo = PositionThesesRepository(db.make_connect(lambda: agent.db_path))
    service = PositionThesisService(repo, TraderService(agent), artifact)
    drafts = {"client": _fake_client(DRAFT)}
    app.dependency_overrides[get_position_thesis_service] = lambda: service
    app.dependency_overrides[get_thesis_draft_client] = lambda: drafts["client"]
    try:
        yield SimpleNamespace(
            pid=pf.id,
            db=agent.db_path,
            service=service,
            drafts=drafts,
            agent=agent,
            artifact=artifact,
        )
    finally:
        app.dependency_overrides.clear()


def _url(stack: SimpleNamespace, suffix: str = "", ticker: str = "AAA") -> str:
    return f"/portfolios/{stack.pid}/theses/{ticker}{suffix}"


def _rows(stack: SimpleNamespace) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(stack.db)
    rows = conn.execute(
        "SELECT id, version, text_source, active, confirmed_at IS NOT NULL, "
        "rules_json FROM position_theses ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def _ledger(stack: SimpleNamespace) -> list[list[tuple[Any, ...]]]:
    conn = sqlite3.connect(stack.db)
    dump = [
        conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in LEDGER_TABLES
    ]
    conn.close()
    return dump


def _save(stack: SimpleNamespace, **fields: Any) -> Any:
    data: dict[str, Any] = {
        "rationale": "Stage 2 leader.",
        "expected_setup": "Holds its 50-day SMA.",
        "review_date": "",
        "rule_kind": ["close_below_sma", "", ""],
        "rule_period": ["50", "", ""],
        "rule_min_rel_volume": ["", "", ""],
        "rule_min_score": ["", "", ""],
        **fields,
    }
    return client.post(_url(stack), data=data, headers=AUTH)


def test_every_route_requires_auth(stack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    cross = {"Sec-Fetch-Site": "cross-site"}

    assert client.get(_url(stack), headers=cross).status_code == 403
    for suffix in ("", "/draft", "/confirm"):
        resp = client.post(_url(stack, suffix), data={"thesis_id": "1"}, headers=cross)
        assert resp.status_code == 403
    assert _rows(stack) == []


def test_editor_renders_the_modal_read_only(stack) -> None:
    before = _ledger(stack)
    resp = client.get(_url(stack), headers=AUTH)

    assert resp.status_code == 200
    assert 'id="thesisEditorModal"' in resp.text
    assert "Thesis — AAA" in resp.text
    assert "No active thesis for this holding yet." in resp.text
    assert not re.search(r"#1[34]\b|GH-", resp.text)
    assert _rows(stack) == [] and _ledger(stack) == before


def test_save_adds_an_active_version_evaluated_now(stack) -> None:
    before = _ledger(stack)
    resp = _save(stack, review_date="2020-01-01")

    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "portfolio-agents-refresh"
    assert 'id="thesisEditorModal"' not in resp.text  # body swap only
    assert 'id="thesis-editor-body"' in resp.text
    assert "Thesis saved as version 1." in resp.text
    assert "Your wording" in resp.text
    assert "Review due" in resp.text
    # The invalidated evaluation names the exact rule, field, session and run.
    assert "Invalidated" in resp.text
    assert (
        "rule 1 close_below_sma (price, sma50) · session 2026-09-25 · "
        "analysis run run-1"
    ) in resp.text
    assert _rows(stack) == [
        (
            1,
            1,
            "user",
            1,
            1,
            '[{"kind":"close_below_sma","period":50,"min_rel_volume":null}]',
        )
    ]
    assert _ledger(stack) == before


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"rule_kind": ["", ""]}, "Add at least one rule"),
        ({"rule_kind": ["moon_phase"]}, "Rule 1:"),
        ({"rule_period": ["20"]}, "Rule 1:"),
        ({"rule_kind": ["score_below"], "rule_min_score": ["11"]}, "Rule 1:"),
        ({"rationale": ""}, "rationale"),
        ({"review_date": "not-a-date"}, "review date"),
    ],
    ids=["no-rules", "unknown-kind", "period-20", "score-11", "blank", "date"],
)
def test_invalid_save_rerenders_with_the_error_and_writes_nothing(
    stack, fields: dict[str, Any], message: str
) -> None:
    resp = _save(stack, **fields)

    assert resp.status_code == 200
    assert "HX-Trigger" not in resp.headers
    assert "alert-warning" in resp.text and message in resp.text
    assert _rows(stack) == []


def test_ai_draft_is_stored_inactive_with_only_valid_rules(stack) -> None:
    before = _ledger(stack)
    resp = client.post(_url(stack, "/draft"), headers=AUTH)

    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "portfolio-agents-refresh"
    assert "AI-drafted wording" in resp.text
    assert "Confirm draft" in resp.text
    assert "AAA leads its group." in resp.text
    [(_, version, source, active, confirmed, rules)] = _rows(stack)
    assert (version, source, active, confirmed) == (1, "ai_draft", 0, 0)
    assert [rule["kind"] for rule in json.loads(rules)] == [
        "close_below_sma",
        "score_below",
    ]
    # Not active, so not evaluated.
    assert stack.service.statuses(stack.pid)["AAA"].latest is None
    assert _ledger(stack) == before


@pytest.mark.parametrize(
    "payload", [{"rationale": "x"}, {**DRAFT, "rules": [{"kind": "moon"}]}]
)
def test_ai_draft_unavailable_writes_nothing(stack, payload: Any) -> None:
    stack.drafts["client"] = _fake_client(payload)
    resp = client.post(_url(stack, "/draft"), headers=AUTH)

    assert resp.status_code == 200
    assert "AI draft unavailable" in resp.text
    assert "HX-Trigger" not in resp.headers
    assert _rows(stack) == []


def test_confirm_makes_the_draft_the_only_active_version(stack) -> None:
    _save(stack)
    client.post(_url(stack, "/draft"), headers=AUTH)
    before = _ledger(stack)

    resp = client.post(_url(stack, "/confirm"), data={"thesis_id": "2"}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "portfolio-agents-refresh"
    assert "Draft confirmed" in resp.text
    assert [(row[0], row[3], row[4]) for row in _rows(stack)] == [
        (1, 0, 1),
        (2, 1, 1),
    ]
    summary = stack.service.statuses(stack.pid)["AAA"]
    assert summary.pending is None
    assert summary.latest is not None and summary.latest.thesis_id == 2
    assert _ledger(stack) == before


def test_confirming_a_stale_version_is_404(stack) -> None:
    client.post(_url(stack, "/draft"), headers=AUTH)
    _save(stack)  # a later save supersedes the draft

    for thesis_id in (1, 2, 99):
        resp = client.post(
            _url(stack, "/confirm"), data={"thesis_id": str(thesis_id)}, headers=AUTH
        )
        assert resp.status_code == 404
    assert [row[3] for row in _rows(stack)] == [0, 1]


def test_security_not_held_is_404(stack) -> None:
    assert client.get(_url(stack, ticker="ZZZ"), headers=AUTH).status_code == 404
    other = f"/portfolios/{stack.pid + 1}/theses/AAA"
    assert client.get(other, headers=AUTH).status_code == 404
    for suffix in ("", "/draft", "/confirm"):
        resp = client.post(
            _url(stack, suffix, ticker="ZZZ"),
            data={"thesis_id": "1", "rationale": "x"},
            headers=AUTH,
        )
        assert resp.status_code == 404
    assert _rows(stack) == []


def test_portfolio_evaluation_appends_once_per_run(stack) -> None:
    _save(stack)
    before = _ledger(stack)
    records, meta = stack.service.published()
    assert meta is not None

    # Save already evaluated this run, so the scan step adds nothing new...
    assert stack.service.evaluate_all(records, meta).count == 0
    # ...until a new run is published.
    next_run = meta.model_copy(update={"run_id": "run-2"})
    assert stack.service.evaluate_all(records, next_run).count == 1
    assert stack.service.evaluate_all(records, next_run).count == 0
    assert _ledger(stack) == before


def _publish(stack: SimpleNamespace, run_id: str) -> None:
    payload = build_analysis_payload(
        [make_record(price=94.0).model_dump(mode="json")],
        run_id=run_id,
        generated_at=datetime.now(UTC),
    )
    stack.artifact.write_text(json.dumps(payload), encoding="utf-8")


def test_not_held_and_stale_404s_carry_a_swappable_warning(stack) -> None:
    get = client.get(_url(stack, ticker="ZZZ"), headers=AUTH)
    post = client.post(_url(stack, "/draft", ticker="ZZZ"), headers=AUTH)
    client.post(_url(stack, "/draft"), headers=AUTH)
    _save(stack)
    stale = client.post(_url(stack, "/confirm"), data={"thesis_id": "1"}, headers=AUTH)

    notice = "This security is no longer held in this portfolio."
    assert get.status_code == 404 and notice in get.text
    assert 'id="thesisEditorModal"' in get.text  # the whole modal on GET
    assert post.status_code == 404 and notice in post.text
    assert 'id="thesis-editor-body"' in post.text
    assert 'id="thesisEditorModal"' not in post.text
    assert stale.status_code == 404
    assert "That draft is no longer pending." in stale.text


def test_editor_forms_swap_404s_and_disable_while_in_flight(stack) -> None:
    client.post(_url(stack, "/draft"), headers=AUTH)  # adds the confirm form
    html = client.get(_url(stack), headers=AUTH).text
    templates = Path(__file__).resolve().parents[1] / "app/api/templates"
    tab = (templates / "_portfolio.html").read_text(encoding="utf-8")

    handler = 'hx-on::before-swap="thesisSwapNotFound(event)"'
    assert html.count("<form ") == html.count('hx-disabled-elt="find button"') == 3
    # htmx fires before-swap on the swap target, not the requesting form or
    # button, so the handler sits on the stable modal target they all swap
    # into (or inside).
    assert handler not in html
    row = tab.split('hx-get="/portfolios/{{ portfolio_id }}/theses/', 1)[1]
    row = row.split("</button>", 1)[0]
    assert handler not in row
    script = (templates / "index.html").read_text(encoding="utf-8")
    assert handler in _modal_target(script)
    assert "function thesisSwapNotFound(event)" in script
    assert "event.detail.xhr.status === 404" in script
    assert "event.detail.shouldSwap = true;" in script
    assert "event.detail.isError = false;" in script


def test_absurd_relative_volume_is_rejected(stack) -> None:
    for volume in ("inf", "nan", "10.5"):
        resp = _save(stack, rule_min_rel_volume=[volume])

        assert resp.status_code == 200
        assert "alert-warning" in resp.text and "Rule 1:" in resp.text
    assert _rows(stack) == []


def test_save_survives_a_failing_immediate_evaluation(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("app.services.position_thesis_service.evaluate_thesis", boom)
    resp = _save(stack)
    client.post(_url(stack, "/draft"), headers=AUTH)
    confirm = client.post(
        _url(stack, "/confirm"), data={"thesis_id": "2"}, headers=AUTH
    )

    assert resp.status_code == 200 and "Thesis saved as version 1." in resp.text
    assert confirm.status_code == 200 and "Draft confirmed" in confirm.text
    assert [row[3] for row in _rows(stack)] == [0, 1]
    assert stack.service.statuses(stack.pid)["AAA"].latest is None


def test_ai_draft_reveals_the_holdings_display_symbol(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    trader = stack.service._trader
    real = trader.get_portfolio
    monkeypatch.setattr(
        trader,
        "get_portfolio",
        lambda **kw: [
            p.model_copy(update={"display_ticker": "AAA.X"}) for p in real(**kw)
        ],
    )

    resp = client.post(_url(stack, "/draft"), headers=AUTH)

    assert "AAA.X leads its group." in resp.text
    assert stack.service.statuses(stack.pid)["AAA"].pending is not None


def test_unheld_thesis_is_skipped_and_stays_active_until_held_again(
    stack,
) -> None:
    _save(stack)
    stack.agent.record_sell("AAA", 10, 95.0, "2026-02-02", portfolio_id=stack.pid)
    records, meta = stack.service.published()
    assert meta is not None

    next_run = meta.model_copy(update={"run_id": "run-2"})
    assert stack.service.evaluate_all(records, next_run).count == 0

    assert [row[3] for row in _rows(stack)] == [1]  # untouched, still active
    assert len(stack.service._repo.history(stack.pid, "AAA")) == 1
    stack.agent.record_buy("AAA", 5, 96.0, "2026-03-03", portfolio_id=stack.pid)
    assert stack.service.evaluate_all(records, next_run).count == 1
    assert stack.service.statuses(stack.pid)["AAA"].latest is not None


def test_failing_holdings_read_raises_and_writes_nothing(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(stack)
    records, meta = stack.service.published()
    assert meta is not None

    def down(**_kw: Any) -> Any:
        raise RuntimeError("trades db down")

    monkeypatch.setattr(stack.service._trader, "get_portfolio", down)
    next_run = meta.model_copy(update={"run_id": "run-2"})
    with pytest.raises(RuntimeError):
        stack.service.evaluate_portfolio(stack.pid, records, next_run)
    # evaluate_all isolates the failing portfolio and names it.
    result = stack.service.evaluate_all(records, next_run)
    assert (result.count, result.failed) == (0, (stack.pid,))
    assert [row[3] for row in _rows(stack)] == [1]
    assert len(stack.service._repo.history(stack.pid, "AAA")) == 1


def test_corrupt_row_and_failing_portfolio_do_not_stop_the_others(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack.agent.record_buy("BBB", 5, 50.0, "2026-01-02", portfolio_id=stack.pid)
    other = stack.agent.create_portfolio("GIA")
    _save(stack)
    conn = sqlite3.connect(stack.db)
    conn.execute(
        "INSERT INTO position_theses (portfolio_id, security_id, version, "
        "rationale, expected_setup, rules_json, text_source, active, "
        "created_at) VALUES (?, 'BBB', 1, 'r', 's', '[{\"kind\": 1}]', "
        "'user', 1, 'n')",
        (stack.pid,),
    )
    conn.commit()
    conn.close()
    trader = stack.service._trader
    real = trader.get_portfolio

    def flaky(portfolio_id: int | None = None, **kw: Any) -> Any:
        if portfolio_id == other.id:
            raise RuntimeError("boom")
        return real(portfolio_id=portfolio_id, **kw)

    monkeypatch.setattr(trader, "get_portfolio", flaky)
    records, meta = stack.service.published()
    assert meta is not None

    next_run = meta.model_copy(update={"run_id": "run-2"})
    result = stack.service.evaluate_all(records, next_run)
    assert (result.count, result.failed) == (1, (other.id,))
    assert set(stack.service.statuses(stack.pid)) == {"AAA"}


def test_result_of_an_earlier_run_is_not_shown_as_current(stack) -> None:
    _save(stack)  # invalidated against run-1
    assert stack.service.statuses(stack.pid)["AAA"].current is True

    _publish(stack, "run-2")
    summary = stack.service.statuses(stack.pid)["AAA"]

    assert summary.current is False
    assert thesis_cell(summary) == AgentCell(
        "Evidence limited", "warn", "last checked on an earlier run"
    )
    stack.artifact.write_text("not json", encoding="utf-8")  # fail-soft
    assert stack.service.statuses(stack.pid)["AAA"].current is False


def test_review_due_uses_the_local_date(stack, monkeypatch: pytest.MonkeyPatch) -> None:
    class Today(date):
        @classmethod
        def today(cls) -> Today:
            return cls(2030, 1, 2)

    monkeypatch.setattr("app.services.position_thesis_service.date", Today)
    _save(stack, review_date="2030-01-02")
    assert stack.service.statuses(stack.pid)["AAA"].review_due is False
    _save(stack, review_date="2030-01-01")
    assert stack.service.statuses(stack.pid)["AAA"].review_due is True


def test_corrupt_evaluation_row_is_skipped_by_statuses_and_editor(stack) -> None:
    _save(stack)  # evaluated against run-1
    conn = sqlite3.connect(stack.db)
    conn.execute(
        "INSERT INTO thesis_evaluations (thesis_id, analysis_run_id, status, "
        "facts_json, evaluated_at) VALUES (1, 'run-0', 'confirmed', '{}', 'n')"
    )
    conn.execute(
        "UPDATE thesis_evaluations SET facts_json = 'not json' "
        "WHERE analysis_run_id = 'run-1'"
    )
    conn.commit()
    conn.close()

    assert stack.service.statuses(stack.pid)["AAA"].latest is None
    assert stack.service.editor(stack.pid, "AAA").history == ()
    assert client.get(_url(stack), headers=AUTH).status_code == 200


def test_draft_superseded_by_a_save_during_the_model_call_is_discarded(
    stack,
) -> None:
    def create(**_kwargs: Any) -> SimpleNamespace:
        _save(stack)  # the user saves while Claude is drafting
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(DRAFT))],
        )

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    stack.drafts["client"] = ThesisDraftClient(api_key="test-key", client=fake)

    resp = client.post(_url(stack, "/draft"), headers=AUTH)

    assert resp.status_code == 200
    assert (
        "You saved while the draft was being written, so the draft was discarded."
    ) in resp.text
    assert "AI draft unavailable" not in resp.text
    assert "HX-Trigger" not in resp.headers
    assert [(row[2], row[3]) for row in _rows(stack)] == [("user", 1)]
    assert stack.service.statuses(stack.pid)["AAA"].pending is None


def test_draft_without_an_analysis_record_says_so_and_writes_nothing(
    stack,
) -> None:
    calls: list[Any] = []

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise AssertionError("no model call without a record")

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    stack.drafts["client"] = ThesisDraftClient(api_key="test-key", client=fake)
    stack.agent.record_buy("BBB", 5, 50.0, "2026-01-02", portfolio_id=stack.pid)

    resp = client.post(_url(stack, "/draft", ticker="BBB"), headers=AUTH)

    assert resp.status_code == 200
    assert (
        "No published analysis for this holding yet, so there is nothing to draft from."
    ) in resp.text
    assert "AI draft unavailable" not in resp.text
    assert "HX-Trigger" not in resp.headers
    assert calls == [] and _rows(stack) == []


def test_duplicate_rules_are_saved_once(stack) -> None:
    resp = _save(
        stack,
        rule_kind=["close_below_sma", "stage_2_lost", "close_below_sma"],
        rule_period=["50", "", "50"],
    )

    assert "Thesis saved as version 1." in resp.text
    [(*_, rules)] = _rows(stack)
    assert [rule["kind"] for rule in json.loads(rules)] == [
        "close_below_sma",
        "stage_2_lost",
    ]


def test_score_threshold_of_one_is_rejected(stack) -> None:
    resp = _save(stack, rule_kind=["score_below"], rule_min_score=["1"])

    assert "alert-warning" in resp.text and "Rule 1:" in resp.text
    assert _rows(stack) == []


TEMPLATES = Path(__file__).resolve().parents[1] / "app/api/templates"
FAILED = "That thesis request failed. Nothing was changed on screen; try again."


def _index() -> str:
    return (TEMPLATES / "index.html").read_text(encoding="utf-8")


def _modal_target(index: str) -> str:
    """Return the opening tag of the stable thesis modal target."""
    return index.split('<div id="thesis-editor-modal-target"', 1)[1].split(">")[0]


def _header(html: str) -> str:
    return html.split("<h6>Active thesis", 1)[1].split("</h6>", 1)[0]


def _evidence_heading(html: str) -> str:
    return html.split("<h6>Evidence", 1)[1].split("</h6>", 1)[0]


def test_editor_header_carries_the_cell_note_and_dates_the_evidence(
    stack,
) -> None:
    _save(stack, review_date="2020-01-01")  # evaluated against run-1
    client.post(_url(stack, "/draft"), headers=AUTH)
    current = client.get(_url(stack), headers=AUTH).text

    assert "Review due" in _header(current)
    assert "Draft to confirm" in _header(current)
    assert "earlier run" not in _evidence_heading(current)

    _publish(stack, "run-2")
    stale = client.get(_url(stack), headers=AUTH).text

    header = _header(stale)
    assert "Evidence limited" in header
    assert "last checked on an earlier run" in header
    assert "last checked on an earlier run" in _evidence_heading(stale)


def test_relative_volume_input_accepts_any_step(stack) -> None:
    html = client.get(_url(stack), headers=AUTH).text
    inputs = re.findall(r'<input[^>]*name="rule_min_rel_volume"[^>]*>', html)

    assert inputs and all('step="any"' in tag for tag in inputs)
    assert all('min="1"' in tag and 'max="10"' in tag for tag in inputs)
    resp = _save(
        stack,
        rule_kind=["close_below_sma"],
        rule_period=["50"],
        rule_min_rel_volume=["1.25"],
    )
    assert "Thesis saved as version 1." in resp.text


def test_failed_thesis_requests_show_an_alert() -> None:
    index = _index()
    tab = (TEMPLATES / "_portfolio.html").read_text(encoding="utf-8")
    row = tab.split('hx-get="/portfolios/{{ portfolio_id }}/theses/', 1)[1]
    row = row.split("</button>", 1)[0]

    for tag in (_modal_target(index), row):
        assert 'hx-on::response-error="thesisRequestFailed(event)"' in tag
        assert 'hx-on::send-error="thesisRequestFailed(event)"' in tag
    assert "function thesisRequestFailed(event)" in index
    assert FAILED in index
    # In the open editor the alert goes into its body; with no editor open
    # (the row button failed) a minimal modal is shown instead.
    assert "event.target.closest('#thesis-editor-body')" in index
    template = index.split('<template id="thesis-error-modal">', 1)[1]
    template = template.split("</template>", 1)[0]
    assert 'id="thesis-editor-body"' in template
    assert "modal.content.cloneNode(true)" in index
    assert not re.search(r"#1[34]\b|GH-", FAILED)


def test_only_html_404s_are_swapped(stack) -> None:
    index = _index()
    handler = index.split("function thesisSwapNotFound(event)", 1)[1]
    handler = handler.split("\n  }\n", 1)[0]

    assert "getResponseHeader('Content-Type')" in handler
    assert "type.startsWith('text/html')" in handler
    notice = client.get(_url(stack, ticker="ZZZ"), headers=AUTH)
    assert notice.status_code == 404
    assert notice.headers["content-type"].startswith("text/html")
    unknown = client.get(_url(stack, "/nope"), headers=AUTH)
    assert unknown.status_code == 404
    assert unknown.headers["content-type"].startswith("application/json")


def test_save_warns_that_it_replaces_a_pending_draft(stack) -> None:
    note = "Saving replaces the pending AI draft."
    assert note not in client.get(_url(stack), headers=AUTH).text

    client.post(_url(stack, "/draft"), headers=AUTH)
    html = client.get(_url(stack), headers=AUTH).text

    edit = html.split('aria-label="Edit thesis"', 1)[1]
    assert edit.index("Save thesis") < edit.index(note) < edit.index("Draft with AI")


def test_editor_announces_swaps_and_manages_focus(stack) -> None:
    html = client.get(_url(stack), headers=AUTH).text
    body = client.post(_url(stack, "/draft"), headers=AUTH).text

    # The live region is outside the swapped body, so it is never replaced.
    status = re.search(r'<div[^>]*id="thesis-editor-status"[^>]*>', html)
    assert status and 'aria-live="polite"' in status.group(0)
    assert html.index('id="thesis-editor-status"') < html.index(
        'id="thesis-editor-body"'
    )
    assert 'id="thesis-editor-status"' not in body
    assert "aria-live" not in body
    assert 'id="thesisEditorModalLabel" tabindex="-1"' in html
    index = _index()
    assert 'hx-on::after-swap="thesisEditorSwapped(event)"' in _modal_target(index)
    swapped = index.split("function thesisEditorSwapped(event)", 1)[1]
    swapped = swapped.split("\n  }\n", 1)[0]
    assert "getElementById('thesis-editor-status')" in swapped
    assert ".focus()" in swapped
    # Closing returns focus to the row button that opened the editor.
    assert "thesisOpener = event.target;" in index
    assert "addEventListener('hidden.bs.modal'" in index
    assert "thesisOpener.focus();" in index
