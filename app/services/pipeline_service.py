"""Pipeline service — runs the scan/analyse/alert workflow once.

Both the web ``/refresh-data`` endpoint and the scheduler invoke the pipeline
through here instead of duplicating the run wiring. It shells out to the
orchestrator's ``--once`` entry point so the run executes in its own process
(matching the previous web behaviour).
"""

import subprocess
import sys
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from pydantic import BaseModel

from app.core.config import ANALYSIS_PROGRESS_TXT, ROOT_DIR

_RUN_TIMEOUT_SECONDS = 1800  # 30 min safety cap; a normal run is far shorter
_status_lock = threading.Lock()
_run_lock = threading.Lock()
_status: dict[str, str] = {"state": "idle", "message": "Ready to refresh data"}

# The web entry point does not otherwise load .env (the subprocess does), but
# preflight must inspect the same configuration the pipeline will receive.
load_dotenv()


class PipelineRunResult(BaseModel):
    """Outcome of a single pipeline run."""

    success: bool
    details: str


class PipelineService:
    """Runs the momentum pipeline once and reports the outcome."""

    def run_once(self) -> PipelineRunResult:
        """Run the orchestrator pipeline once and capture its output."""
        if not _run_lock.acquire(blocking=False):
            return PipelineRunResult(
                success=False,
                details="A pipeline refresh is already running.",
            )
        try:
            return self._run_once_locked()
        finally:
            _run_lock.release()

    def _run_once_locked(self) -> PipelineRunResult:
        """Run one refresh while holding the process-wide single-run lock."""
        with _status_lock:
            _status.update(
                state="running",
                message="Scanning sources and market data…",
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        ANALYSIS_PROGRESS_TXT.unlink(missing_ok=True)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "app.orchestration.orchestrator", "--once"],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            outcome = PipelineRunResult(
                success=False,
                details=f"Pipeline timed out after {_RUN_TIMEOUT_SECONDS}s",
            )
            self._set_finished(outcome)
            return outcome
        details = (result.stdout or result.stderr).strip()
        outcome = PipelineRunResult(success=result.returncode == 0, details=details)
        self._set_finished(outcome)
        return outcome

    @staticmethod
    def missing_configuration() -> list[dict[str, str]]:
        """Return non-blocking pipeline capability warnings without secrets."""
        import os

        warnings: list[dict[str, str]] = []
        if not os.getenv("FMP_API_KEY"):
            warnings.append(
                {
                    "name": "FMP API key",
                    "impact": "The S&P 500 VCP screener will be skipped.",
                }
            )
        if not os.getenv("ALPHA_VANTAGE_API_KEY"):
            warnings.append(
                {
                    "name": "Alpha Vantage API key",
                    "impact": "Missing Yahoo fundamental fields will not be backfilled.",
                }
            )
        if not all(
            os.getenv(key) for key in ("EMAIL_USER", "EMAIL_PASSWORD", "EMAIL_TO")
        ):
            warnings.append(
                {
                    "name": "Email alert settings",
                    "impact": "Summary emails and alert delivery are disabled.",
                }
            )
        return warnings

    @staticmethod
    def _set_finished(outcome: PipelineRunResult) -> None:
        with _status_lock:
            _status.update(
                state="complete" if outcome.success else "failed",
                message="Refresh complete" if outcome.success else outcome.details,
            )

    @staticmethod
    def status() -> dict[str, str]:
        """Return the current run state and the latest analyst progress line."""
        with _status_lock:
            result = dict(_status)
        if result["state"] == "running" and ANALYSIS_PROGRESS_TXT.exists():
            lines = ANALYSIS_PROGRESS_TXT.read_text(encoding="utf-8").splitlines()
            if lines:
                result["message"] = lines[-1]
        return result
