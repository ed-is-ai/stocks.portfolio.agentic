"""Pipeline acquisition behavior shared by scheduler and web subprocesses."""

import pytest

import app.orchestration.orchestrator as orchestrator
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.pipeline_status import PipelineState


@pytest.mark.parametrize("requested_run_id", [None, "web-child-run"])
def test_direct_pipeline_skips_when_another_run_is_active(
    monkeypatch, tmp_path, requested_run_id
) -> None:
    path = tmp_path / "pipeline_status.json"
    repo = PipelineStatusRepository(path)
    repo.start(run_id="web-run")
    if requested_run_id is None:
        monkeypatch.delenv("PIPELINE_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_RUN_ID", requested_run_id)
    monkeypatch.setattr(orchestrator, "PIPELINE_STATUS_JSON", path)
    monkeypatch.setattr(
        orchestrator,
        "load_watchlist",
        lambda: (_ for _ in ()).throw(AssertionError("pipeline work must not run")),
    )
    monkeypatch.setattr(orchestrator, "_append_run_log", lambda entry: None)

    ran = orchestrator.pipeline(force=True)

    assert ran is False
    retained = repo.load()
    assert retained.run_id == "web-run"
    assert retained.state is PipelineState.RUNNING
