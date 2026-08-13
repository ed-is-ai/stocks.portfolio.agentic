from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from app.services.backtest.strategy_job import (
    JobFailureCode,
    StrategyJobStatus,
    StrategyJobType,
)
from app.services.backtest.worker import main
import app.services.backtest.worker as worker_module


@dataclass
class Result:
    status: StrategyJobStatus


class Engine:
    def __init__(self, status: StrategyJobStatus) -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def run(self, job_id: str, claim_token: str):
        self.calls.append((job_id, claim_token))
        return Result(self.status)


def test_worker_dispatches_exact_claim_and_returns_success_for_complete() -> None:
    engine = Engine(StrategyJobStatus.COMPLETE)

    exit_code = main(
        ["--job-id", "job-1", "--claim-token", "claim-1"],
        engine_factory=lambda _job_id: engine,
    )

    assert exit_code == 0
    assert engine.calls == [("job-1", "claim-1")]


def test_worker_returns_failure_for_failed_authoritative_state() -> None:
    engine = Engine(StrategyJobStatus.FAILED)
    assert (
        main(
            ["--job-id", "job-1", "--claim-token", "claim-1"],
            engine_factory=lambda _job_id: engine,
        )
        == 1
    )


def test_worker_rejects_unknown_arguments() -> None:
    with pytest.raises(SystemExit):
        main(["--job-id", "job-1", "--claim-token", "claim-1", "--extra"])


@dataclass
class ClaimedJob:
    id: str = "job-1"
    status: StrategyJobStatus = StrategyJobStatus.RUNNING
    job_type: StrategyJobType = StrategyJobType.BACKTEST
    claim_token: str = "claim-1"
    status_version: int = 2
    cancel_requested_at: object | None = None


class Repository:
    def __init__(self, job: ClaimedJob | None = None) -> None:
        self.job = job or ClaimedJob()
        self.failures: list[dict[str, object]] = []

    def strategy_job(self, _job_id: str):
        return self.job

    def fail_claimed_strategy_job(self, _job_id, _claim_token, **kwargs):
        self.failures.append(kwargs)
        self.job = replace(self.job, status=StrategyJobStatus.FAILED)
        return self.job


def test_production_dispatch_rejects_backtest_with_stable_integrity_failure() -> None:
    repo = Repository()

    assert (
        main(
            ["--job-id", "job-1", "--claim-token", "claim-1"],
            repository_factory=lambda: repo,  # type: ignore[arg-type]
        )
        == 1
    )

    assert repo.failures[0]["failure_code"] is JobFailureCode.INTEGRITY_ERROR


def test_engine_construction_failure_is_not_mislabeled_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Repository(ClaimedJob(job_type=StrategyJobType.INITIALIZATION))

    def broken(*_args, **_kwargs):
        raise RuntimeError("corrupt profile")

    monkeypatch.setattr(worker_module, "build_initialization_engine", broken)

    assert (
        main(
            ["--job-id", "job-1", "--claim-token", "claim-1"],
            repository_factory=lambda: repo,  # type: ignore[arg-type]
        )
        == 1
    )
    assert repo.failures[0]["failure_code"] is JobFailureCode.INTEGRITY_ERROR
    assert repo.failures[0]["detail"] == "Strategy worker configuration is invalid"
