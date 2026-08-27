from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import threading
import sqlite3

import pytest

from app.services.backtest.strategy_job_service import (
    StrategyJobService,
    dispatcher_lock_backoff_seconds,
    is_transient_sqlite_lock,
)
from app.services.backtest.strategy_job import (
    BacktestSubmissionV1,
    BootstrapSubmissionV1,
    InitializationSubmissionV1,
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobRestartV1,
    WorkerLeaseFenceV1,
    WorkerLeaseV1,
)


NOW = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)


@dataclass
class FakeJob:
    id: str
    status_version: int
    status: str = "running"
    claim_token: str = "claim-1"


@dataclass
class FakeClaim:
    job: FakeJob
    claim_token: str


class FakeRepository:
    """Mirror only the repository surface the dispatcher itself calls."""

    def __init__(self, claim: FakeClaim | None) -> None:
        self.claim = claim
        self.failed: list[tuple[str, str, dict[str, object]]] = []
        self.reconciled = ()
        self.created: list[object] = []
        self.generation = 1
        self.lease_conflict = False
        self.acquisitions: list[str] = []
        self.reconcile_fences: list[WorkerLeaseFenceV1 | None] = []
        self.claim_fences: list[WorkerLeaseFenceV1 | None] = []

    def acquire_or_renew_worker_lease(
        self, instance_id: str, *, ttl_seconds: float
    ) -> WorkerLeaseV1:
        self.acquisitions.append(instance_id)
        if self.lease_conflict:
            raise StrategyJobConflict("worker lease is held by another live instance")
        return WorkerLeaseV1(
            instance_id=instance_id,
            generation=self.generation,
            heartbeat_at=NOW,
            expires_at=NOW + timedelta(seconds=ttl_seconds),
        )

    def create_initialization_job(self, **configuration):
        self.created.append(configuration)
        return "queued"

    def create_backtest_job(self, submission):
        self.created.append(submission)
        return "backtest-queued"

    def restart_backtest_job(self, source_job_id, *, expected_version, idempotency_key):
        self.created.append((source_job_id, expected_version, idempotency_key))
        return "backtest-restarted"

    def claim_next_strategy_job(self, *, lease: WorkerLeaseFenceV1 | None = None):
        self.claim_fences.append(lease)
        result, self.claim = self.claim, None
        return result

    def strategy_job(self, job_id: str):
        return FakeJob(job_id, 3)

    def fail_claimed_strategy_job(self, *args, **kwargs):
        self.failed.append((str(args[0]), str(args[1]), kwargs))
        return None

    def reconcile_interrupted_strategy_jobs(
        self, *, lease: WorkerLeaseFenceV1 | None = None
    ):
        self.reconcile_fences.append(lease)
        return self.reconciled


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.waited = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_dispatch_spawns_exact_module_worker_without_shell_or_pipes() -> None:
    repo = FakeRepository(FakeClaim(FakeJob("job-1", 2), "claim-1"))
    calls: list[tuple[list[str], dict[str, object]]] = []
    process = FakeProcess()

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    service = StrategyJobService(repo, popen=popen, project_root=Path("/project"))

    assert service.dispatch_once() is True
    assert len(calls) == 1
    argv, kwargs = calls[0]
    # Compare resolved paths, not literal strings: a venv's "python" and
    # "python3" aliases both resolve to the same interpreter binary, and
    # which alias sys.executable reports can vary by launch mechanism.
    assert Path(argv[0]).resolve() == Path(sys.executable).resolve()
    assert argv[1:] == [
        "-m",
        "app.services.backtest.worker",
        "--job-id",
        "job-1",
        "--claim-token",
        "claim-1",
    ]
    assert kwargs == {"cwd": "/project"}
    assert service.dispatch_once() is False


def test_spawn_failure_conditionally_fails_claim() -> None:
    repo = FakeRepository(FakeClaim(FakeJob("job-1", 2), "claim-1"))

    def broken(*_args, **_kwargs):
        raise OSError("spawn details must not leak")

    service = StrategyJobService(repo, popen=broken, project_root=Path("/project"))

    assert service.dispatch_once() is False
    assert len(repo.failed) == 1
    job_id, claim_token, kwargs = repo.failed[0]
    args = (job_id, claim_token)
    assert args == ("job-1", "claim-1")
    assert kwargs["expected_version"] == 2
    assert kwargs["failure_code"] is JobFailureCode.WORKER_INTERRUPTED
    assert kwargs["detail"] == "Worker process could not be started"


def test_nonterminal_child_exit_gets_fallback_failure() -> None:
    repo = FakeRepository(FakeClaim(FakeJob("job-1", 2), "claim-1"))
    process = FakeProcess(returncode=1)
    service = StrategyJobService(
        repo, popen=lambda *_a, **_k: process, project_root=Path("/project")
    )
    assert service.dispatch_once() is True

    assert service.dispatch_once() is False
    assert len(repo.failed) == 1
    assert repo.failed[0][-1]["detail"] == "Worker exited before terminal state"


def test_shutdown_terminates_only_owned_child_and_marks_interrupted() -> None:
    repo = FakeRepository(FakeClaim(FakeJob("job-1", 2), "claim-1"))
    process = FakeProcess()
    service = StrategyJobService(
        repo, popen=lambda *_a, **_k: process, project_root=Path("/project")
    )
    service.dispatch_once()

    service.shutdown()

    assert process.terminated and process.waited
    assert len(repo.failed) == 1


def test_enqueue_requires_current_qualification_before_repository_write() -> None:
    repo = FakeRepository(None)
    service = StrategyJobService(repo, qualification_digest=lambda: None)

    with pytest.raises(StrategyJobConflict):
        service.enqueue_initialization(
            InitializationSubmissionV1(
                profile_hash="a" * 64,
                requested_start="2026-05",
                requested_end="2026-05",
                calendar_dataset_version="exchange-calendars-v1",
            )
        )

    assert repo.created == []


def test_qualified_enqueue_returns_without_running_the_worker_inline() -> None:
    repo = FakeRepository(None)
    service = StrategyJobService(repo, qualification_digest=lambda: "b" * 64)

    submission = InitializationSubmissionV1(
        profile_hash="a" * 64,
        requested_start="2026-05",
        requested_end="2026-05",
        calendar_dataset_version="exchange-calendars-v1",
    )
    assert service.enqueue_initialization(submission) == "queued"
    assert repo.created == [
        {
            **submission.model_dump(),
            "qualification_contract_digest": "b" * 64,
        }
    ]


def test_enqueue_backtest_delegates_directly_to_the_repository() -> None:
    repo = FakeRepository(None)
    service = StrategyJobService(repo)

    submission = BacktestSubmissionV1(
        strategy_id="momentum_v1",
        strategy_api_version=1,
        strategy_source_digest="7" * 64,
        parameters={"lookback": 20},
        profile_hash="a" * 64,
        start_month="2026-05",
        end_month="2026-05",
        base_currency="USD",
        starting_capital=Decimal("10000"),
        run_input_manifest_digest="9" * 64,
        execution_contract_digest="8" * 64,
        canonical_manifest_json="{}",
    )

    assert service.enqueue_backtest(submission) == "backtest-queued"
    assert repo.created == [submission]


def test_restart_backtest_delegates_directly_to_the_repository() -> None:
    repo = FakeRepository(None)
    service = StrategyJobService(repo)

    result = service.restart_backtest(
        StrategyJobRestartV1(
            source_job_id="job-1", expected_version=3, idempotency_key="retry-1"
        )
    )

    assert result == "backtest-restarted"
    assert repo.created == [("job-1", 3, "retry-1")]


def test_dispatch_loop_survives_one_repository_error() -> None:
    repo = FakeRepository(None)
    service = StrategyJobService(repo)
    calls = 0

    def dispatch() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        service._stop.set()
        return False

    service.dispatch_once = dispatch  # type: ignore[method-assign]
    thread = threading.Thread(target=service._dispatch_loop, args=(0.001,))
    thread.start()
    thread.join(timeout=1)

    assert calls == 2


def test_sqlite_lock_classifier_is_narrow() -> None:
    assert is_transient_sqlite_lock(sqlite3.OperationalError("database is locked"))
    assert not is_transient_sqlite_lock(sqlite3.OperationalError("no such table"))
    assert not is_transient_sqlite_lock(RuntimeError("database is locked"))


def test_sqlite_lock_backoff_is_exponential_and_capped() -> None:
    assert dispatcher_lock_backoff_seconds(0) == 0.25
    assert dispatcher_lock_backoff_seconds(1) == 0.5
    assert dispatcher_lock_backoff_seconds(99) == 5.0


def test_dispatcher_defers_after_locked_heartbeat_without_dispatching(caplog) -> None:
    service = StrategyJobService(FakeRepository(None))
    dispatched = False

    def locked_heartbeat() -> None:
        service._stop.set()
        raise sqlite3.OperationalError("database is locked")

    def dispatch() -> bool:
        nonlocal dispatched
        dispatched = True
        return False

    service._heartbeat = locked_heartbeat  # type: ignore[method-assign]
    service.dispatch_once = dispatch  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        service._dispatch_loop(0.001)

    assert not dispatched
    assert "another process is writing it" in caplog.text


def test_shutdown_kills_child_after_graceful_wait_failure() -> None:
    class StubbornProcess(FakeProcess):
        def wait(self, timeout=None):
            self.waited = True
            if not self.killed:
                raise TimeoutError("still running")
            return self.returncode

        def terminate(self):
            self.terminated = True

    repo = FakeRepository(FakeClaim(FakeJob("job-1", 2), "claim-1"))
    process = StubbornProcess()
    service = StrategyJobService(
        repo, popen=lambda *_a, **_k: process, project_root=Path("/project")
    )
    service.dispatch_once()

    service.shutdown()

    assert process.terminated and process.killed
    assert len(repo.failed) == 1


# ---------------------------------------------------------------------------
# Story 4.1: lease acquisition, generation-fenced dispatch, and takeover.
# ---------------------------------------------------------------------------


def test_startup_takes_the_lease_then_reconciles_fenced_by_its_generation() -> None:
    repo = FakeRepository(None)
    service = StrategyJobService(repo, instance_id="worker-a")

    service.reconcile_startup()

    assert repo.acquisitions == ["worker-a"]
    assert repo.reconcile_fences == [
        WorkerLeaseFenceV1(instance_id="worker-a", generation=1)
    ]


def test_dispatch_hands_the_worker_its_owner_instance_and_lease_generation() -> None:
    repo = FakeRepository(FakeClaim(FakeJob("job-1", 2), "claim-1"))
    calls: list[list[str]] = []
    service = StrategyJobService(
        repo,
        popen=lambda argv, **_k: (calls.append(argv), FakeProcess())[1],
        project_root=Path("/project"),
        instance_id="worker-a",
    )
    service.acquire_lease()

    assert service.dispatch_once() is True

    fence = WorkerLeaseFenceV1(instance_id="worker-a", generation=1)
    assert repo.claim_fences == [fence]
    assert calls[0][-4:] == [
        "--owner-instance-id",
        "worker-a",
        "--lease-generation",
        "1",
    ]


def test_unleased_dispatch_omits_the_fence_arguments() -> None:
    repo = FakeRepository(FakeClaim(FakeJob("job-1", 2), "claim-1"))
    calls: list[list[str]] = []
    service = StrategyJobService(
        repo,
        popen=lambda argv, **_k: (calls.append(argv), FakeProcess())[1],
        project_root=Path("/project"),
    )

    assert service.dispatch_once() is True

    assert repo.claim_fences == [None]
    assert "--owner-instance-id" not in calls[0]


def test_heartbeat_renewal_at_the_same_generation_reconciles_nothing() -> None:
    repo = FakeRepository(None)
    service = StrategyJobService(repo, instance_id="worker-a")
    service.acquire_lease()

    service._heartbeat()

    assert repo.reconcile_fences == []
    assert service.lease is not None and service.lease.generation == 1


def test_heartbeat_that_retakes_the_lease_reconciles_the_inherited_claims() -> None:
    repo = FakeRepository(None)
    service = StrategyJobService(repo, instance_id="worker-a")
    service.acquire_lease()

    repo.generation = 2
    service._heartbeat()

    assert repo.reconcile_fences == [
        WorkerLeaseFenceV1(instance_id="worker-a", generation=2)
    ]
    assert service.lease is not None and service.lease.generation == 2


def test_interrupted_owned_child_is_failed_under_the_current_fence() -> None:
    repo = FakeRepository(FakeClaim(FakeJob("job-1", 2), "claim-1"))
    process = FakeProcess(returncode=1)
    service = StrategyJobService(
        repo,
        popen=lambda *_a, **_k: process,
        project_root=Path("/project"),
        instance_id="worker-a",
    )
    service.acquire_lease()
    assert service.dispatch_once() is True

    assert service.dispatch_once() is False

    assert repo.failed[0][-1]["lease"] == WorkerLeaseFenceV1(
        instance_id="worker-a", generation=1
    )


def test_stage_activities_enqueue_through_their_own_repository_writes() -> None:
    class StageRepository(FakeRepository):
        def create_bootstrap_job(self, submission):
            self.created.append(("bootstrap", submission))
            return "bootstrap-queued"

        def create_preparation_job(self, *, parent_job_id=None):
            self.created.append(("preparation", parent_job_id))
            return "preparation-queued"

    repo = StageRepository(None)
    service = StrategyJobService(repo)

    submission = BootstrapSubmissionV1(idempotency_key="bootstrap-submit")
    assert service.enqueue_bootstrap(submission) == "bootstrap-queued"
    assert service.enqueue_preparation(parent_job_id="job-1") == "preparation-queued"
    assert repo.created == [
        ("bootstrap", submission),
        ("preparation", "job-1"),
    ]


def test_startup_against_a_live_foreign_lease_reconciles_nothing() -> None:
    repo = FakeRepository(None)
    repo.lease_conflict = True
    service = StrategyJobService(repo, instance_id="worker-b")

    assert service.reconcile_startup() == ()

    assert service.lease is None
    assert repo.reconcile_fences == []


def test_dispatcher_takes_a_lease_it_could_not_get_at_startup() -> None:
    repo = FakeRepository(None)
    repo.lease_conflict = True
    service = StrategyJobService(repo, instance_id="worker-b")
    service.reconcile_startup()

    repo.lease_conflict = False
    service._heartbeat()

    assert service.lease is not None
    assert repo.reconcile_fences == [
        WorkerLeaseFenceV1(instance_id="worker-b", generation=1)
    ]
