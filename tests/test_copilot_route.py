"""Route tests for POST /copilot/ask and the scanner's "Why?" button (GH-13)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agents.research import copilot as copilot_module
from app.agents.research.copilot import ResearchCopilotClient
from app.api.app import app
from app.api.dependencies import get_research_copilot_client, get_trader_service
from app.api.routes import copilot as copilot_route
from app.api.templating import templates
from app.core.alerting import build_alert_ui_state
from app.core.recommendation import classify_recommendation
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.record import StockRecord
from app.schemas.scan import StockAnalysis

ROOT = Path(__file__).resolve().parents[1]


def _record() -> StockRecord:
    return StockRecord(
        ticker="ZETA",
        as_of="2026-09-25",
        price=50.0,
        volume=1000,
        rel_volume=1.5,
        high_52w=55.0,
        low_52w=30.0,
        pct_from_52w_high=-9.0,
        pct_change_week=1.0,
        analysis=StockAnalysis(
            score=8,
            stage="Stage 2",
            entry_zone="broken_out",
            volume_confirmed=True,
            summary="ZETA broke out",
        ),
    )


def _fake_client(payload: dict[str, Any] | None) -> tuple[ResearchCopilotClient, list]:
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        )

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    key = "test-key" if payload is not None else ""
    return ResearchCopilotClient(api_key=key, client=fake), calls


@pytest.fixture
def post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return a poster wired to a tmp analysis artifact and a tmp audit log."""
    audit = tmp_path / "logs" / "copilot_audit.jsonl"
    artifact = tmp_path / "analysis_results.json"
    payload = build_analysis_payload(
        [_record().model_dump(mode="json"), {"ticker": "BROKEN"}],
        run_id="run-7",
        generated_at=datetime.now(timezone.utc),
    )
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(copilot_module, "COPILOT_AUDIT_JSONL", audit)
    monkeypatch.setattr(copilot_route, "ANALYSIS_JSON", artifact)
    monkeypatch.setattr(copilot_route, "load_source_health", lambda: {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("APP_AUTH_TOKEN", "test-token")
    trader = MagicMock()
    trader.held_tickers.return_value = set()
    app.dependency_overrides[get_trader_service] = lambda: trader

    def _post(client: ResearchCopilotClient, data: dict[str, str]):
        app.dependency_overrides[get_research_copilot_client] = lambda: client
        return TestClient(app).post(
            "/copilot/ask", data=data, headers={"X-Auth-Token": "test-token"}
        )

    yield _post, audit
    app.dependency_overrides.clear()


def test_answered_panel_names_run_model_ticker_and_citations(post) -> None:
    send, audit = post
    client, calls = _fake_client(
        {
            "answer": "Security A broke out on volume.",
            "citations": ["E1", "E3", "E99"],
            "unknowns": ["No news was supplied."],
        }
    )

    response = send(client, {"ticker": "zeta"})

    assert response.status_code == 200
    html = response.text
    assert "ZETA broke out on volume." in html
    assert 'data-citation="E1"' in html and 'data-citation="E3"' in html
    assert "E99" not in html
    assert "No news was supplied." in html
    assert "claude-sonnet-5" in html and "run-7" in html
    assert "ZETA" not in calls[0]["messages"][0]["content"]
    assert len(audit.read_text().splitlines()) == 1


def test_unavailable_panel_lists_evidence(post) -> None:
    send, audit = post
    client, calls = _fake_client(None)

    response = send(client, {"ticker": "ZETA", "question": "Why?"})

    assert response.status_code == 200
    assert "Copilot unavailable" in response.text
    assert "E1" in response.text
    assert calls == []
    assert json.loads(audit.read_text())["status"] == "unavailable"


def test_ticker_without_analysis_makes_no_llm_call(post) -> None:
    send, audit = post
    client, calls = _fake_client({"answer": "x", "citations": [], "unknowns": []})

    response = send(client, {"ticker": "NOPE"})

    assert response.status_code == 200
    assert "No analysis for NOPE" in response.text
    assert calls == []
    assert json.loads(audit.read_text())["status"] == "no_analysis"


@pytest.mark.parametrize("ticker", ["", "   "])
def test_blank_ticker_is_422_without_audit(post, ticker: str) -> None:
    send, audit = post
    client, calls = _fake_client({"answer": "x", "citations": [], "unknowns": []})

    response = send(client, {"ticker": ticker})

    assert response.status_code == 422
    assert calls == []
    assert not audit.exists()


def test_ask_requires_local_or_token(post, monkeypatch: pytest.MonkeyPatch) -> None:
    send, _ = post
    client, _ = _fake_client(None)
    app.dependency_overrides[get_research_copilot_client] = lambda: client

    response = TestClient(app).post(
        "/copilot/ask", data={"ticker": "ZETA"}, headers={"X-Auth-Token": "bad"}
    )

    assert response.status_code == 403


def test_scanner_row_has_why_button_and_index_has_panel() -> None:
    record = _record()
    html = templates.get_template("_stock_scanner.html").render(
        records=[record],
        portfolio_tickers=set(),
        recommendations={"ZETA": classify_recommendation(record)},
        alert_states={
            "ZETA": build_alert_ui_state(
                record, has_watching=False, last_alerted_at=None
            )
        },
    )
    index = (ROOT / "app" / "api" / "templates" / "index.html").read_text()

    assert 'hx-post="/copilot/ask"' in html
    assert 'hx-vals=\'{"ticker": "ZETA"}\'' in html
    assert 'data-bs-target="#copilotPanel"' in html
    assert 'id="copilotPanel"' in index and 'id="copilot-body"' in index
    assert "function copilotStatus" in index


def _ui_assertions(markup: str) -> None:
    assert 'hx-sync="#copilot-body:replace"' in markup
    assert "hx-on::before-request=\"copilotStatus('Asking…')\"" in markup
    assert "hx-on::response-error=\"copilotStatus('Copilot request failed." in markup


def test_why_button_and_ask_form_reset_panel_and_sync(post) -> None:
    send, _ = post
    client, _ = _fake_client(None)
    panel = send(client, {"ticker": "ZETA"}).text
    scanner = (ROOT / "app" / "api" / "templates" / "_stock_scanner.html").read_text()

    _ui_assertions(panel)
    _ui_assertions(scanner)
    assert "hx-disabled-elt=\"find button[type='submit']\"" in panel


def test_portfolio_target_keeps_the_panel_in_the_aside(post) -> None:
    """GH-19: the Portfolio aside's follow-ups swap into its own body."""
    send, _ = post
    client, _ = _fake_client(None)
    panel = send(client, {"ticker": "ZETA", "target": "#portfolio-copilot-body"}).text

    assert 'hx-target="#portfolio-copilot-body"' in panel
    assert 'hx-sync="#portfolio-copilot-body:replace"' in panel
    assert 'name="target" value="#portfolio-copilot-body"' in panel
    assert "copilotStatus('Asking…', '', '#portfolio-copilot-body')" in panel
    assert 'hx-target="#copilot-body"' not in panel


@pytest.mark.parametrize(
    "target", ["#evil", "#tab-content", '#copilot-body" onclick="x']
)
def test_unknown_target_falls_back_to_the_offcanvas(post, target: str) -> None:
    send, _ = post
    client, _ = _fake_client(None)
    panel = send(client, {"ticker": "ZETA", "target": target}).text

    assert 'hx-target="#copilot-body"' in panel
    assert target not in panel
    _ui_assertions(panel)


def test_overlong_target_is_rejected(post) -> None:
    send, audit = post
    client, calls = _fake_client(None)

    response = send(client, {"ticker": "ZETA", "target": "#" + "x" * 32})

    assert response.status_code == 422
    assert calls == [] and not audit.exists()
