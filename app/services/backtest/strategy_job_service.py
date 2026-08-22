"""Process-local dispatcher for the durable Strategy Manager FIFO ledger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import threading
import logging
from typing import Callable, Protocol
from uuid import uuid4

from app.core.config import ROOT_DIR
from app.services.backtest.strategy_job import (
    BacktestSubmissionV1,
    BootstrapSubmissionV1,
    InitializationSubmissionV1,
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobStatus,
    StrategyJobCancellationV1,
    StrategyJobDeletionV1,
    StrategyJobRestartV1,
    StrategyJobActionV1,
    WorkerLeaseFenceV1,
    WorkerLeaseV1,
)

logger = logging.getLogger(__name__)

#: How long an acquired worker lease stays valid without a heartbeat.
#: Deliberately several dispatcher poll intervals wide so an ordinary
#: scheduling hiccup never lets a second instance evict a healthy worker.
DEFAULT_LEASE_TTL_SECONDS = 30.0


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
        instance_id: str | None = None,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self._repository = repository
        self._popen = popen
        self._project_root = project_root
        self._qualification_digest = qualification_digest or (
            lambda: self._repository.current_qualification_contract_digest()
        )
        self._instance_id = instance_id or str(uuid4())
        self._lease_ttl_seconds = lease_ttl_seconds
        self._lease: WorkerLeaseV1 | None = None
        self._owned: _OwnedChild | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def instance_id(self) -> str:
        """This process's stable worker identity for the singleton lease."""
        return self._instance_id

    @property
    def lease(self) -> WorkerLeaseV1 | None:
        """The lease this service currently believes it holds, if any."""
        return self._lease

    def acquire_lease(self) -> WorkerLeaseV1 | None:
        """Acquire, take over, or heartbeat the singleton worker lease.

        A renewal by the current owner keeps its generation, so a
        heartbeat never fences this instance's own in-flight writes out.
        Returns ``None`` while a different live instance still owns the
        lease: this process simply is not the worker, and every dispatcher
        poll retries until that lease expires.
        """
        try:
            self._lease = self._repository.acquire_or_renew_worker_lease(
                self._instance_id, ttl_seconds=self._lease_ttl_seconds
            )
        except StrategyJobConflict:
            self._lease = None
        return self._lease

    def _heartbeat(self) -> None:
        """Renew the lease, reconciling whatever a new generation inherits."""
        previous = self._lease
        lease = self.acquire_lease()
        if lease is not None and (
            previous is None or previous.generation != lease.generation
        ):
            self._repository.reconcile_interrupted_strategy_jobs(lease=lease.fence)

    def _fence(self) -> WorkerLeaseFenceV1 | None:
        return None if self._lease is None else self._lease.fence

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

    def enqueue_backtest(self, submission: BacktestSubmissionV1):
        return self._repository.create_backtest_job(submission)

    def enqueue_bootstrap(self, submission: BootstrapSubmissionV1):
        return self._repository.create_bootstrap_job(submission)

    def enqueue_preparation(self, *, parent_job_id: str | None = None):
        return self._repository.create_preparation_job(parent_job_id=parent_job_id)

    def request_cancellation(self, request: StrategyJobCancellationV1):
        return self._repository.request_strategy_job_cancellation(
            request.job_id, expected_version=request.expected_version
        )

    def restart_initialization(self, request: StrategyJobRestartV1):
        return self._repository.restart_initialization_job(
            request.source_job_id,
            expected_version=request.expected_version,
            idempotency_key=request.idempotency_key,
        )

    def restart_backtest(self, request: StrategyJobRestartV1):
        return self._repository.restart_backtest_job(
            request.source_job_id,
            expected_version=request.expected_version,
            idempotency_key=request.idempotency_key,
        )

    def delete_job(self, request: StrategyJobDeletionV1):
        return self._repository.delete_strategy_job(
            request.job_id, expected_version=request.expected_version
        )

    def legal_actions(self, job_id: str) -> StrategyJobActionV1:
        job = self._repository.strategy_job(job_id)
        return StrategyJobActionV1(
            job_id=job_id,
            expected_version=job.status_version,
            legal_actions=self._repository.legal_strategy_job_actions(job_id),
        )

    def reconcile_startup(self):
        """Take the worker lease, then fail every claim it inherited.

        A ``running`` row owned by the generation just acquired is left
        untouched; every claim an evicted owner abandoned becomes
        ``worker_interrupted``. Reconciles nothing while another live
        instance still owns the lease -- those rows are its business, not
        this process's.
        """
        lease = self.acquire_lease()
        if lease is None:
            return ()
        return self._repository.reconcile_interrupted_strategy_jobs(lease=lease.fence)

    def dispatch_once(self) -> bool:
        """Poll an owned child or claim and spawn one queued job.

        Claims across all four activity types -- the ledger's FIFO is
        keyed on ``enqueue_seq`` alone -- and hands the claimed job's id,
        claim token, and (once a lease is held) the owning instance and
        lease generation to one typed worker child.
        """
        with self._lock:
            if self._owned is not None:
                if self._owned.process.poll() is None:
                    return False
                owned, self._owned = self._owned, None
                self._fallback_if_nonterminal(
                    owned, "Worker exited before terminal state"
                )
                return False

            fence = self._fence()
            claim = self._repository.claim_next_strategy_job(lease=fence)
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
            if fence is not None:
                argv += [
                    "--owner-instance-id",
                    fence.instance_id,
                    "--lease-generation",
                    str(fence.generation),
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
                self._heartbeat()
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
                lease=self._fence(),
            )
        except StrategyJobConflict:
            # A terminal worker write or newer owner won the race.
            return


__all__ = ["DEFAULT_LEASE_TTL_SECONDS", "StrategyJobService"]
