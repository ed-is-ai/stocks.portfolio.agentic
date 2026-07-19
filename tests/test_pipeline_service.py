import subprocess

import app.services.pipeline_service as pipeline_service_module
from app.services.pipeline_service import PipelineService


def test_run_once_success(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        assert kwargs.get("timeout")  # timeout must be passed
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is True
    assert result.details == "ok"
    assert PipelineService.status()["state"] == "complete"


def test_run_once_failure_uses_stderr(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is False
    assert result.details == "boom"
    assert PipelineService.status()["state"] == "failed"


def test_run_once_timeout_returns_failure(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is False
    assert "timed out" in result.details.lower()
    assert PipelineService.status()["state"] == "failed"


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
