from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from app.api.app import app
import app.api.stock_scanner_context as stock_scanner_context_module
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.pipeline_status import PipelineStage, PipelineState, StageState
from app.schemas.source_health import SourceHealth, SourceName, SourceState
import app.services.pipeline_service as pipeline_service_module


client = TestClient(app)


def test_pipeline_status_renders_stages_progress_and_active_polling(
    monkeypatch, tmp_path
) -> None:
    repo = PipelineStatusRepository(tmp_path / "status.json")
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)
    repo.start(run_id="web-run")
    repo.transition(
        PipelineStage.SOURCES, StageState.RUNNING, expected_run_id="web-run"
    )
    repo.transition(
        PipelineStage.SOURCES, StageState.COMPLETE, expected_run_id="web-run"
    )
    repo.transition(
        PipelineStage.ANALYSIS,
        StageState.RUNNING,
        expected_run_id="web-run",
        current=3,
        total=9,
    )

    response = client.get("/pipeline-status")

    assert response.status_code == 200
    assert "Sources" in response.text
    assert "Analysis" in response.text
    assert "3/9" in response.text
    assert 'hx-trigger="every 2s"' in response.text


def test_pipeline_status_terminal_state_stops_polling(monkeypatch, tmp_path) -> None:
    repo = PipelineStatusRepository(tmp_path / "status.json")
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)
    repo.start(run_id="web-run")
    repo.finish(
        PipelineState.PARTIAL,
        expected_run_id="web-run",
        error_summary="One source failed",
    )

    response = client.get("/pipeline-status")

    assert "Pipeline partially complete" in response.text
    assert 'hx-trigger="every 2s"' not in response.text
    assert "One source failed" in response.text


def test_pipeline_status_naive_timestamp_fails_safe_to_idle(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "status.json"
    repo = PipelineStatusRepository(path)
    payload = repo.start(run_id="bad-time").model_dump(mode="json")
    payload["updated_at"] = "2026-07-19T20:00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    response = client.get("/pipeline-status")

    assert response.status_code == 200
    assert "Pipeline idle" in response.text


def test_pipeline_status_shows_unknown_freshness_when_no_artifact_exists(
    monkeypatch, tmp_path
) -> None:
    """No analysis artifact on disk at all -> freshness unknown (#71)."""
    monkeypatch.setattr(
        stock_scanner_context_module, "ANALYSIS_JSON", tmp_path / "missing.json"
    )
    repo = PipelineStatusRepository(tmp_path / "status.json")
    monkeypatch.setattr(stock_scanner_context_module, "_status_repository", repo)
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    response = client.get("/pipeline-status")

    assert "Last successful refresh unknown" in response.text


def test_pipeline_status_shows_freshness_and_toast_for_owning_run(
    monkeypatch, tmp_path
) -> None:
    """Freshness and a breakdown toast reflect the run owning the artifact (#71)."""
    analysis_path = tmp_path / "analysis_results.json"
    generated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    analysis_path.write_text(
        json.dumps(
            build_analysis_payload([], run_id="run-a", generated_at=generated_at)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_scanner_context_module, "ANALYSIS_JSON", analysis_path)

    repo = PipelineStatusRepository(tmp_path / "status.json")
    repo.start(run_id="run-a")
    repo.update_source_health(
        {
            SourceName.TRADINGVIEW_US: SourceHealth(
                source=SourceName.TRADINGVIEW_US,
                state=SourceState.EMPTY,
                count=0,
                display_message="No stocks matched.",
            ),
            SourceName.VCP_FMP: SourceHealth(
                source=SourceName.VCP_FMP,
                state=SourceState.SKIPPED,
                count=0,
                detail_code="missing_configuration",
                display_message="FMP API key is not configured.",
            ),
        },
        expected_run_id="run-a",
    )
    repo.finish(PipelineState.PARTIAL, expected_run_id="run-a", artifact_produced=True)
    monkeypatch.setattr(stock_scanner_context_module, "_status_repository", repo)
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    response = client.get("/pipeline-status")
    markup = response.text

    assert "Last successful refresh" in markup
    assert 'datetime="' in markup
    assert 'id="pipeline-breakdown-toast"' in markup
    assert 'role="status"' in markup
    assert "TradingView US" in markup
    assert "Empty" in markup
    assert "Skipped" in markup


def test_pipeline_status_cached_source_labelled_cached_not_skipped(
    monkeypatch, tmp_path
) -> None:
    """A cached_input source reads 'Cached' inline, not bare 'Skipped' (#103)."""
    analysis_path = tmp_path / "analysis_results.json"
    generated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    analysis_path.write_text(
        json.dumps(
            build_analysis_payload([], run_id="run-c", generated_at=generated_at)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_scanner_context_module, "ANALYSIS_JSON", analysis_path)

    repo = PipelineStatusRepository(tmp_path / "status.json")
    repo.start(run_id="run-c")
    repo.update_source_health(
        {
            SourceName.WHALE_WISDOM: SourceHealth(
                source=SourceName.WHALE_WISDOM,
                state=SourceState.SKIPPED,
                count=45,
                detail_code="cached_input",
                display_message="Using cached WhaleWisdom input; not refreshed.",
            ),
            SourceName.VCP_FMP: SourceHealth(
                source=SourceName.VCP_FMP,
                state=SourceState.SKIPPED,
                count=0,
                detail_code="missing_configuration",
                display_message="FMP API key is not configured.",
            ),
        },
        expected_run_id="run-c",
    )
    repo.finish(PipelineState.PARTIAL, expected_run_id="run-c", artifact_produced=True)
    monkeypatch.setattr(stock_scanner_context_module, "_status_repository", repo)
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    markup = client.get("/pipeline-status").text

    # The cached source names its state and de-emphasises the count.
    assert "WhaleWisdom: Cached" in markup
    assert "source-cached" in markup
    assert "source-count-stale" in markup
    # A genuinely skipped (non-cached) source keeps the "Skipped" label.
    assert "Skipped" in markup
    # Both sources here are Discovery-stage; the funnel heading groups them
    # under a single "Discovery" subheading (#108).
    assert "source-stage-label" in markup
    assert "Discovery" in markup


def test_pipeline_status_failed_attempt_keeps_prior_refresh_and_shows_toast(
    monkeypatch, tmp_path
) -> None:
    """A failed latest attempt must not hide the last usable refresh time (#71)."""
    analysis_path = tmp_path / "analysis_results.json"
    generated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    analysis_path.write_text(
        json.dumps(
            build_analysis_payload([], run_id="usable", generated_at=generated_at)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_scanner_context_module, "ANALYSIS_JSON", analysis_path)

    repo = PipelineStatusRepository(tmp_path / "status.json")
    repo.start(run_id="usable")
    repo.finish(
        PipelineState.COMPLETE, expected_run_id="usable", artifact_produced=True
    )
    repo.start(run_id="failed")
    repo.finish(
        PipelineState.FAILED,
        expected_run_id="failed",
        error_summary="Latest provider request failed.",
    )
    monkeypatch.setattr(stock_scanner_context_module, "_status_repository", repo)
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    response = client.get("/pipeline-status")
    markup = response.text

    assert "Last successful refresh" in markup
    assert 'id="pipeline-breakdown-toast"' in markup
    assert "Latest refresh failed: Latest provider request failed." in markup


def test_pipeline_status_no_toast_when_coverage_fully_ok(monkeypatch, tmp_path) -> None:
    """A fully-ok run with no errors needs nothing to disclose (#71)."""
    analysis_path = tmp_path / "analysis_results.json"
    analysis_path.write_text(
        json.dumps(
            build_analysis_payload(
                [], run_id="run-ok", generated_at=datetime.now(timezone.utc)
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_scanner_context_module, "ANALYSIS_JSON", analysis_path)

    repo = PipelineStatusRepository(tmp_path / "status.json")
    repo.start(run_id="run-ok")
    repo.update_source_health(
        {
            SourceName.TRADINGVIEW_US: SourceHealth(
                source=SourceName.TRADINGVIEW_US,
                state=SourceState.OK,
                count=42,
                display_message="42 matches.",
            ),
        },
        expected_run_id="run-ok",
    )
    repo.finish(
        PipelineState.COMPLETE, expected_run_id="run-ok", artifact_produced=True
    )
    monkeypatch.setattr(stock_scanner_context_module, "_status_repository", repo)
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    response = client.get("/pipeline-status")

    assert 'id="pipeline-breakdown-toast"' not in response.text


def test_pipeline_status_no_toast_while_running(monkeypatch, tmp_path) -> None:
    """The breakdown toast waits for a terminal state, not mid-run (#71)."""
    repo = PipelineStatusRepository(tmp_path / "status.json")
    repo.start(run_id="running")
    repo.update_source_health(
        {
            SourceName.TRADINGVIEW_US: SourceHealth(
                source=SourceName.TRADINGVIEW_US,
                state=SourceState.FAILED,
                count=0,
                display_message="Request timed out.",
            ),
        },
        expected_run_id="running",
    )
    monkeypatch.setattr(stock_scanner_context_module, "_status_repository", repo)
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    response = client.get("/pipeline-status")

    assert 'id="pipeline-breakdown-toast"' not in response.text
