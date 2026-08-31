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
    # Fresh is deliberately quiet: detail remains attached to the single
    # keyboard/touch dropdown target, but there is no persistent icon/time.
    assert "bi-clock-fill" not in markup
    assert 'aria-label="Refresh Data"' in markup
    freshness_tag = markup.split('id="refresh-freshness"', 1)[1].split(">", 1)[0]
    assert 'aria-live="polite"' not in freshness_tag
    assert 'aria-describedby="refresh-freshness-description"' in markup
    assert "freshness-fresh refresh-freshness-cue" not in markup
    assert 'id="refresh-data-button"' in markup
    assert '<time class="local-time" datetime="' in markup
    assert 'data-refresh-age="' in markup
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
    assert "bi-exclamation-triangle-fill" in markup
    assert "Analysis data is stale." in markup
    assert "Use it with caution." in markup
    assert "Freshness window ended" in markup


def test_index_renders_unknown_affordance_when_no_artifact(
    monkeypatch, tmp_path
) -> None:
    """No artifact reads explicitly unknown, never blank."""
    _use_artifact(monkeypatch, tmp_path, None)

    markup = client.get("/").text

    assert "freshness-unknown" in markup
    assert "bi-question-circle" in markup
    assert "Last successful refresh unknown" in markup


def test_index_latest_failure_overrides_fresh_colour_but_keeps_last_success(
    monkeypatch, tmp_path
) -> None:
    generated_at = datetime.now(timezone.utc)
    _use_artifact(monkeypatch, tmp_path, generated_at)
    repo = stock_scanner_context_module._status_repository
    repo.start(run_id="failed")
    from app.schemas.pipeline_status import PipelineState

    repo.finish(
        PipelineState.FAILED,
        expected_run_id="failed",
        error_summary="Provider timed out.",
    )

    markup = client.get("/").text

    assert "freshness-failed" in markup
    assert "bi-x-circle-fill" in markup
    assert "Latest refresh failed." in markup
    assert "Provider timed out." in markup
    assert "Last successful refresh:" in markup


def test_index_latest_failure_keeps_stale_caution(monkeypatch, tmp_path) -> None:
    _use_artifact(
        monkeypatch, tmp_path, datetime.now(timezone.utc) - timedelta(days=30)
    )
    repo = stock_scanner_context_module._status_repository
    repo.start(run_id="failed-stale")
    from app.schemas.pipeline_status import PipelineState

    repo.finish(
        PipelineState.FAILED,
        expected_run_id="failed-stale",
        error_summary="Provider timed out.",
    )

    markup = client.get("/").text

    assert "freshness-failed" in markup
    assert "Last usable analysis data is stale; use it with caution." in markup
    assert "Freshness window ended" in markup


def test_index_reserves_no_permanent_space_for_the_status_bar(
    monkeypatch, tmp_path
) -> None:
    """The body offset is conditional on a bar actually being present (#418)."""
    _use_artifact(monkeypatch, tmp_path, datetime.now(timezone.utc))

    markup = client.get("/").text

    assert "body { padding-bottom: calc(2.5rem" not in markup
    assert "body:has(.pipeline-status-bar .pipeline-status)" in markup
