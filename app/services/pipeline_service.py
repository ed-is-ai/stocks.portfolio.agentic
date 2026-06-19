"""Pipeline service — runs the scan/analyse/alert workflow once.

Both the web ``/refresh-data`` endpoint and the scheduler invoke the pipeline
through here instead of duplicating the run wiring. It shells out to the
orchestrator's ``--once`` entry point so the run executes in its own process
(matching the previous web behaviour).
"""

import subprocess
import sys

from pydantic import BaseModel

from app.core.config import ROOT_DIR

_RUN_TIMEOUT_SECONDS = 1800  # 30 min safety cap; a normal run is far shorter


class PipelineRunResult(BaseModel):
    """Outcome of a single pipeline run."""

    success: bool
    details: str


class PipelineService:
    """Runs the momentum pipeline once and reports the outcome."""

    def run_once(self) -> PipelineRunResult:
        """Run the orchestrator pipeline once and capture its output."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "app.orchestration.orchestrator", "--once"],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return PipelineRunResult(
                success=False,
                details=f"Pipeline timed out after {_RUN_TIMEOUT_SECONDS}s",
            )
        details = (result.stdout or result.stderr).strip()
        return PipelineRunResult(success=result.returncode == 0, details=details)
