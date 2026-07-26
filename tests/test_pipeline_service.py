import subprocess

import app.services.pipeline_service as pipeline_service_module
from app.repositories.pipeline_status_repo import PipelineStatusRepository
from app.schemas.pipeline_status import PipelineStage, PipelineState, StageState
from app.services.pipeline_service import PipelineService


def _use_status_repo(monkeypatch, tmp_path) -> PipelineStatusRepository:
    repo = PipelineStatusRepository(tmp_path / "pipeline_status.json")
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)
    return repo


def test_run_once_success(monkeypatch, tmp_path) -> None:
    repo = _use_status_repo(monkeypatch, tmp_path)

    def fake_run(*args, **kwargs):
        assert kwargs.get("timeout")  # timeout must be passed
        assert kwargs["env"]["PIPELINE_RUN_ID"]
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is True
    assert result.details == "ok"
    assert PipelineService.status()["state"] == "complete"
    assert repo.load().run_id is not None


def test_run_once_omits_extract_flag_by_default(monkeypatch, tmp_path) -> None:
    _use_status_repo(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    PipelineService().run_once()
    assert "--extract" not in captured["argv"]


def test_run_once_passes_extract_flag_when_requested(monkeypatch, tmp_path) -> None:
    _use_status_repo(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    PipelineService().run_once(extract=True)
    assert "--extract" in captured["argv"]


def test_run_once_failure_uses_stderr(monkeypatch, tmp_path) -> None:
    _use_status_repo(monkeypatch, tmp_path)

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is False
    assert result.details == "boom"
    assert PipelineService.status()["state"] == "failed"


def test_nonzero_exit_overrides_premature_complete_status(
    monkeypatch, tmp_path
) -> None:
    repo = _use_status_repo(monkeypatch, tmp_path)

    def fake_run(*args, **kwargs):
        run_id = kwargs["env"]["PIPELINE_RUN_ID"]
        repo.transition(
            PipelineStage.EXPORT, StageState.RUNNING, expected_run_id=run_id
        )
        repo.transition(
            PipelineStage.EXPORT, StageState.COMPLETE, expected_run_id=run_id
        )
        repo.finish(PipelineState.COMPLETE, expected_run_id=run_id)
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="", stderr="log failed"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()

    assert result.success is False
    assert repo.load().state is PipelineState.FAILED


def test_run_once_timeout_returns_failure(monkeypatch, tmp_path) -> None:
    repo = _use_status_repo(monkeypatch, tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is False
    assert "timed out" in result.details.lower()
    assert PipelineService.status()["state"] == "failed"
    assert repo.load().completed_at is not None


def test_status_reads_persisted_cross_process_progress(monkeypatch, tmp_path) -> None:
    repo = _use_status_repo(monkeypatch, tmp_path)
    repo.start(run_id="from-subprocess")
    repo.transition(
        PipelineStage.ANALYSIS,
        StageState.RUNNING,
        expected_run_id="from-subprocess",
        current=7,
        total=20,
    )

    status = PipelineService.status()

    assert status["run_id"] == "from-subprocess"
    assert status["current_stage"] == "analysis"
    assert status["analysis_current"] == 7
    assert status["analysis_total"] == 20


def test_unexpected_subprocess_exception_sets_terminal_failure(
    monkeypatch, tmp_path
) -> None:
    repo = _use_status_repo(monkeypatch, tmp_path)

    def fake_run(*args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()

    assert result.success is False
    assert "cannot start" in result.details
    assert repo.load().state is PipelineState.FAILED


def test_older_service_completion_does_not_terminate_newer_run(
    monkeypatch, tmp_path
) -> None:
    repo = _use_status_repo(monkeypatch, tmp_path)

    def fake_run(*args, **kwargs):
        old_run_id = kwargs["env"]["PIPELINE_RUN_ID"]
        repo.finish(PipelineState.COMPLETE, expected_run_id=old_run_id)
        repo.start(run_id="run-b")
        repo.finish(PipelineState.COMPLETE, expected_run_id="run-b")
        return subprocess.CompletedProcess(
            args, returncode=0, stdout="old ok", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()

    retained = repo.load()
    assert result.success is False
    assert "no longer owns" in result.details
    assert retained.run_id == "run-b"
    assert retained.state is PipelineState.COMPLETE


def test_external_active_run_is_refused_without_spawning(monkeypatch, tmp_path) -> None:
    repo = _use_status_repo(monkeypatch, tmp_path)
    repo.start(run_id="scheduled-run")
    spawned = False

    def fake_run(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("subprocess must not start")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()

    assert result.success is False
    assert "already running" in result.details.lower()
    assert spawned is False
    retained = repo.load()
    assert retained.run_id == "scheduled-run"
    assert retained.state is PipelineState.RUNNING


def test_run_once_rejects_concurrent_refresh() -> None:
    acquired = pipeline_service_module._run_lock.acquire(blocking=False)
    assert acquired is True
    try:
        result = PipelineService().run_once()
    finally:
        pipeline_service_module._run_lock.release()

    assert result.success is False
    assert "already running" in result.details.lower()


def test_missing_configuration_reports_capability_impacts(monkeypatch) -> None:
    for key in (
        "FMP_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "EMAIL_USER",
        "EMAIL_PASSWORD",
        "EMAIL_TO",
    ):
        monkeypatch.delenv(key, raising=False)

    warnings = PipelineService.missing_configuration()

    assert [warning["name"] for warning in warnings] == [
        "FMP API key",
        "Alpha Vantage API key",
        "Email alert settings",
    ]
