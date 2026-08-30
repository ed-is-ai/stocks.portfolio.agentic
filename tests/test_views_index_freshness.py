"""`GET /` renders the header freshness affordance on first paint (#418)."""

from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from app.api.app import app
import app.api.stock_scanner_context as stock_scanner_context_module
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.analysis_artifact import build_analysis_payload


client = TestClient(app)


def _use_artifact(monkeypatch, tmp_path, generated_at: datetime | None) -> None:
    """Point the freshness context at a temp artifact of the given age."""
    analysis_path = tmp_path / "analysis_results.json"
    if generated_at is not None:
        analysis_path.write_text(
            json.dumps(
                build_analysis_payload([], run_id="run", generated_at=generated_at)
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(stock_scanner_context_module, "ANALYSIS_JSON", analysis_path)
    monkeypatch.setattr(
        stock_scanner_context_module,
        "_status_repository",
        PipelineStatusRepository(tmp_path / "status.json"),
    )


def test_index_renders_fresh_affordance_beside_refresh_control(
    monkeypatch, tmp_path
) -> None:
    """A recent artifact reads fresh, with a localisable timestamp."""
    _use_artifact(monkeypatch, tmp_path, datetime.now(timezone.utc))

    markup = client.get("/").text

    assert 'id="refresh-freshness"' in markup
    assert "freshness-fresh" in markup
    assert "bi-clock-fill" in markup
    assert 'data-bs-toggle="tooltip"' in markup
    assert 'aria-label="Analysis freshness"' in markup
    # Keyboard users must be able to reach the tooltip, and a bare <span> is
    # role=generic where ARIA forbids naming -- role="note" makes the label
    # and the descendant copy both exposable.
    assert 'role="note" tabindex="0"' in markup
    # The cue plays on the server-rendered first paint only.
    assert "refresh-freshness-cue" in markup
    assert 'id="refresh-data-button"' in markup
    assert '<time class="local-time" datetime="' in markup
    assert "Last successful refresh" in markup


def test_index_renders_stale_affordance_with_caution_sentence(
    monkeypatch, tmp_path
) -> None:
    """A long-stale artifact reads stale, with the caution copy for SR users."""
    _use_artifact(
        monkeypatch, tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
    )

    markup = client.get("/").text

    assert "freshness-stale" in markup
    assert "bi-exclamation-circle-fill" in markup
    assert "Analysis data is stale and should be used with caution." in markup


def test_index_renders_unknown_affordance_when_no_artifact(
    monkeypatch, tmp_path
) -> None:
    """No artifact reads explicitly unknown, never blank."""
    _use_artifact(monkeypatch, tmp_path, None)

    markup = client.get("/").text

    assert "freshness-unknown" in markup
    assert "bi-question-circle" in markup
    assert "Last successful refresh unknown" in markup


def test_index_reserves_no_permanent_space_for_the_status_bar(
    monkeypatch, tmp_path
) -> None:
    """The body offset is conditional on a bar actually being present (#418)."""
    _use_artifact(monkeypatch, tmp_path, datetime.now(timezone.utc))

    markup = client.get("/").text

    assert "body { padding-bottom: calc(2.5rem" not in markup
    assert "body:has(.pipeline-status-bar .pipeline-status)" in markup
