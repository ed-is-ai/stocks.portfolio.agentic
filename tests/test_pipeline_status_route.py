import json

from fastapi.testclient import TestClient

from app.api.app import app
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.pipeline_status import PipelineStage, PipelineState, StageState
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


def test_pipeline_status_naive_timestamp_fails_safe_to_idle(monkeypatch, tmp_path) -> None:
    path = tmp_path / "status.json"
    repo = PipelineStatusRepository(path)
    payload = repo.start(run_id="bad-time").model_dump(mode="json")
    payload["updated_at"] = "2026-07-19T20:00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    response = client.get("/pipeline-status")

    assert response.status_code == 200
    assert "Pipeline idle" in response.text
