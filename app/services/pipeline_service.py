"""Pipeline service — runs the scan/analyse/alert workflow once.

Both the web ``/refresh-data`` endpoint and the scheduler invoke the pipeline
through here instead of duplicating the run wiring. It shells out to the
orchestrator's ``--once`` entry point so the run executes in its own process
(matching the previous web behaviour).
"""

import subprocess
import sys
import threading
import os
from uuid import uuid4

from dotenv import load_dotenv
from pydantic import BaseModel

from app.core.config import (
    PIPELINE_RUN_TIMEOUT_SECONDS,
    PIPELINE_STALE_GRACE_SECONDS,
    PIPELINE_STATUS_JSON,
    ROOT_DIR,
)
from app.repositories.pipeline_status_repo import (
    PipelineRunActiveError,
    PipelineStatusRepository,
)
from app.schemas.pipeline_status import PipelineStage, PipelineState

_RUN_TIMEOUT_SECONDS = PIPELINE_RUN_TIMEOUT_SECONDS
_run_lock = threading.Lock()
_status_repository = PipelineStatusRepository(
    PIPELINE_STATUS_JSON,
    stale_after_seconds=_RUN_TIMEOUT_SECONDS + PIPELINE_STALE_GRACE_SECONDS,
)

# The web entry point does not otherwise load .env (the subprocess does), but
# preflight must inspect the same configuration the pipeline will receive.
load_dotenv()


class PipelineRunResult(BaseModel):
    """Outcome of a single pipeline run."""

    success: bool
    details: str


class PipelineService:
    """Runs the momentum pipeline once and reports the outcome."""

    def run_once(self, extract: bool = False) -> PipelineRunResult:
        """Run the orchestrator pipeline once and capture its output.

        When ``extract`` is true the run passes ``--extract`` to the
        orchestrator so WhaleWisdom/StockTwits are pulled fresh instead of
        reusing the cached extraction file.
        """
        if not _run_lock.acquire(blocking=False):
            return PipelineRunResult(
                success=False,
                details="A pipeline refresh is already running.",
            )
        try:
            return self._run_once_locked(extract=extract)
        finally:
            _run_lock.release()

    def _run_once_locked(self, extract: bool = False) -> PipelineRunResult:
        """Run one refresh while holding the process-wide single-run lock."""
        run_id = str(uuid4())
        try:
            _status_repository.start(run_id=run_id)
        except PipelineRunActiveError as error:
            return PipelineRunResult(success=False, details=str(error))
        environment = os.environ.copy()
        environment["PIPELINE_RUN_ID"] = run_id
        argv = [sys.executable, "-m", "app.orchestration.orchestrator", "--once"]
        if extract:
            argv.append("--extract")
        try:
            result = subprocess.run(
                argv,
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT_SECONDS,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            outcome = PipelineRunResult(
                success=False,
                details=f"Pipeline timed out after {_RUN_TIMEOUT_SECONDS}s",
            )
            self._set_finished(outcome, run_id)
            return outcome
        except Exception as error:
            outcome = PipelineRunResult(success=False, details=str(error))
            self._set_finished(outcome, run_id)
            return outcome
        details = (result.stdout or result.stderr).strip()
        persisted = _status_repository.load()
        owns_status = persisted.run_id == run_id
        success = (
            owns_status
            and result.returncode == 0
            and persisted.state is not PipelineState.FAILED
        )
        if not owns_status:
            details = (
                f"Pipeline run {run_id} no longer owns the status record; "
                f"active run is {persisted.run_id}."
            )
        outcome = PipelineRunResult(success=success, details=details)
        self._set_finished(outcome, run_id)
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
    def _set_finished(outcome: PipelineRunResult, run_id: str) -> None:
        current = _status_repository.load()
        if current.run_id != run_id:
            return
        if not outcome.success and current.state is not PipelineState.FAILED:
            _status_repository.finish(
                PipelineState.FAILED,
                expected_run_id=run_id,
                error_summary=outcome.details,
            )
        elif outcome.success and current.state is PipelineState.RUNNING:
            _status_repository.finish(
                PipelineState.COMPLETE,
                expected_run_id=run_id,
            )

    @staticmethod
    def status() -> dict[str, object]:
        """Return the persisted cross-process run status for web rendering."""
        status = _status_repository.load()
        result = status.model_dump(mode="json")
        for item, model in zip(result["stages"], status.stages, strict=True):
            item["label"] = model.stage.label
        analysis = status.stage(PipelineStage.ANALYSIS)
        result["analysis_current"] = analysis.current
        result["analysis_total"] = analysis.total
        return result
