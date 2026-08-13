"""Process-local dispatcher for the durable Strategy Manager FIFO ledger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import threading
import logging
from typing import Callable, Protocol

from app.core.config import ROOT_DIR
from app.services.backtest.strategy_job import (
    InitializationSubmissionV1,
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobStatus,
    StrategyJobCancellationV1,
)

logger = logging.getLogger(__name__)


class ProcessLike(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int | None: ...


@dataclass(frozen=True)
class _OwnedChild:
    job_id: str
    claim_token: str
    process: ProcessLike


class StrategyJobService:
    """Own at most one child while SQLite owns cross-process serialization."""

    def __init__(
        self,
        repository,
        *,
        popen: Callable[..., ProcessLike] = subprocess.Popen,
        project_root: Path = ROOT_DIR,
        qualification_digest: Callable[[], str | None] | None = None,
    ) -> None:
        self._repository = repository
        self._popen = popen
        self._project_root = project_root
        self._qualification_digest = qualification_digest or (
            lambda: self._repository.current_qualification_contract_digest()
        )
        self._owned: _OwnedChild | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def enqueue_initialization(self, submission: InitializationSubmissionV1):
        qualification_digest = self._qualification_digest()
        if qualification_digest is None:
            raise StrategyJobConflict(
                "historical data contract is not currently qualified"
            )
        return self._repository.create_initialization_job(
            **submission.model_dump(),
            qualification_contract_digest=qualification_digest,
        )

    def request_cancellation(self, request: StrategyJobCancellationV1):
        return self._repository.request_strategy_job_cancellation(
            request.job_id, expected_version=request.expected_version
        )

    def reconcile_startup(self):
        return self._repository.reconcile_interrupted_strategy_jobs()

    def dispatch_once(self) -> bool:
        """Poll an owned child or claim and spawn one queued job."""
        with self._lock:
            if self._owned is not None:
                if self._owned.process.poll() is None:
                    return False
                owned, self._owned = self._owned, None
                self._fallback_if_nonterminal(
                    owned, "Worker exited before terminal state"
                )
                return False

            claim = self._repository.claim_next_strategy_job()
            if claim is None:
                return False
            argv = [
                sys.executable,
                "-m",
                "app.services.backtest.worker",
                "--job-id",
                claim.job.id,
                "--claim-token",
                claim.claim_token,
            ]
            try:
                process = self._popen(argv, cwd=str(self._project_root))
            except Exception:
                self._fail_owned_claim(
                    claim.job.id,
                    claim.claim_token,
                    claim.job.status_version,
                    "Worker process could not be started",
                )
                return False
            self._owned = _OwnedChild(claim.job.id, claim.claim_token, process)
            return True

    def start_dispatcher(self, *, poll_interval: float = 0.25) -> None:
        if poll_interval <= 0:
            raise ValueError("dispatcher poll interval must be positive")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._dispatch_loop,
                args=(poll_interval,),
                name="strategy-job-dispatcher",
                daemon=True,
            )
            self._thread.start()

    def _dispatch_loop(self, poll_interval: float) -> None:
        while not self._stop.wait(poll_interval):
            try:
                self.dispatch_once()
            except Exception:
                logger.exception("Strategy job dispatcher iteration failed")

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            owned, self._owned = self._owned, None
            if owned is None:
                return
            if owned.process.poll() is None:
                owned.process.terminate()
                try:
                    owned.process.wait(timeout=5)
                except Exception:
                    owned.process.kill()
                    owned.process.wait(timeout=5)
            if owned.process.poll() is None:
                logger.error("Strategy worker did not stop during shutdown")
                return
            self._fallback_if_nonterminal(owned, "Worker interrupted by shutdown")

    def _fallback_if_nonterminal(self, owned: _OwnedChild, detail: str) -> None:
        job = self._repository.strategy_job(owned.job_id)
        if (
            getattr(job, "status", None) in {StrategyJobStatus.RUNNING, "running"}
            and getattr(job, "claim_token", owned.claim_token) == owned.claim_token
        ):
            self._fail_owned_claim(
                owned.job_id,
                owned.claim_token,
                job.status_version,
                detail,
            )

    def _fail_owned_claim(
        self, job_id: str, claim_token: str, version: int, detail: str
    ) -> None:
        try:
            self._repository.fail_claimed_strategy_job(
                job_id,
                claim_token,
                expected_version=version,
                failure_code=JobFailureCode.WORKER_INTERRUPTED,
                failed_month=None,
                detail=detail,
            )
        except StrategyJobConflict:
            # A terminal worker write or newer owner won the race.
            return


__all__ = ["StrategyJobService"]
