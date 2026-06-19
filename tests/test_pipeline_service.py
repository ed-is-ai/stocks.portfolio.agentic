import subprocess

from app.services.pipeline_service import PipelineService


def test_run_once_success(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        assert kwargs.get("timeout")  # timeout must be passed
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is True
    assert result.details == "ok"


def test_run_once_failure_uses_stderr(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is False
    assert result.details == "boom"


def test_run_once_timeout_returns_failure(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is False
    assert "timed out" in result.details.lower()
