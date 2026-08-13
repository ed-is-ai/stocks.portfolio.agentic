from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import threading

import pytest

from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.strategy_job import (
    InitializationSubmissionV1,
    JobFailureCode,
    StrategyJobConflict,
)


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
    def __init__(self, claim: FakeClaim | None) -> None:
        self.claim = claim
        self.failed: list[tuple[str, str, dict[str, object]]] = []
        self.reconciled = ()
        self.created: list[dict[str, object]] = []

    def create_initialization_job(self, **configuration):
        self.created.append(configuration)
        return "queued"

    def claim_next_strategy_job(self):
        result, self.claim = self.claim, None
        return result

    def strategy_job(self, job_id: str):
        return FakeJob(job_id, 3)

    def fail_claimed_strategy_job(self, *args, **kwargs):
        self.failed.append((str(args[0]), str(args[1]), kwargs))
        return None

    def reconcile_interrupted_strategy_jobs(self):
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
    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "app.services.backtest.worker",
                "--job-id",
                "job-1",
                "--claim-token",
                "claim-1",
            ],
            {"cwd": "/project"},
        )
    ]
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
