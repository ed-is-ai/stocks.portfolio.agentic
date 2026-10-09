from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Callable, cast

import pytest

from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.strategy_job import (
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
)
from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.worker import BacktestExecutionEngine
import app.services.backtest.worker as worker_module


class _Repository:
    def __init__(self, status: StrategyJobStatus) -> None:
        self.job = SimpleNamespace(
            job_type=StrategyJobType.BACKTEST,
            status=status,
        )

    def strategy_job(self, _job_id: str):
        return self.job

    def fail_claimed_strategy_job(self, *_args, **_kwargs):
        self.job.status = StrategyJobStatus.FAILED
        return self.job


@pytest.mark.parametrize(
    "status",
    [
        StrategyJobStatus.COMPLETE,
        StrategyJobStatus.FAILED,
        StrategyJobStatus.CANCELLED,
    ],
)
def test_worker_reconciles_terminal_backtest_once(
    monkeypatch: pytest.MonkeyPatch, status: StrategyJobStatus
) -> None:
    repository = _Repository(status)
    engine = object.__new__(BacktestExecutionEngine)
    engine._repository = cast(BacktestRepository, repository)
    run_once = cast(
        Callable[[str, str], StrategyJobV1],
        lambda job_id, claim_token: cast(
            StrategyJobV1, SimpleNamespace(status=status)
        ),
    )
    monkeypatch.setattr(engine, "_run_once", run_once)
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.backtest.strategy_experiment_service.StrategyExperimentService.reconcile_candidate",
        lambda _service, candidate_id: calls.append(candidate_id),
    )

    result = engine.run("candidate-1", "claim-1")

    assert result.status is status
    assert calls == ["candidate-1"]


def test_worker_does_not_reconcile_a_nonterminal_backtest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository(StrategyJobStatus.QUEUED)
    engine = object.__new__(BacktestExecutionEngine)
    engine._repository = cast(BacktestRepository, repository)
    run_once = cast(
        Callable[[str, str], StrategyJobV1],
        lambda job_id, claim_token: cast(
            StrategyJobV1,
            SimpleNamespace(status=StrategyJobStatus.QUEUED),
        ),
    )
    monkeypatch.setattr(engine, "_run_once", run_once)
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.backtest.strategy_experiment_service.StrategyExperimentService.reconcile_candidate",
        lambda _service, candidate_id: calls.append(candidate_id),
    )

    engine.run("candidate-1", "claim-1")

    assert calls == []


def test_worker_reconciles_after_construction_failure_terminalizes_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository(StrategyJobStatus.RUNNING)
    repository.job.id = "candidate-1"
    repository.job.claim_token = "claim-1"
    repository.job.status_version = 2
    repository.job.cancel_requested_at = None
    repository.job.current_month = None

    monkeypatch.setattr(
        worker_module,
        "build_backtest_engine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad pins")),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.backtest.strategy_experiment_service.StrategyExperimentService.reconcile_candidate",
        lambda _service, candidate_id: calls.append(candidate_id),
    )

    result = worker_module.main(
        ["--job-id", "candidate-1", "--claim-token", "claim-1"],
        repository_factory=lambda: cast(BacktestRepository, repository),
    )

    assert result == 1
    assert repository.job.status is StrategyJobStatus.FAILED
    assert calls == ["candidate-1"]


def test_dispatcher_retries_durable_terminal_experiment_after_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PendingRepository:
        def pending_strategy_experiment_reconciliations(self):
            return ("candidate-1",)

    repository = _PendingRepository()
    service = StrategyJobService(cast(BacktestRepository, repository))
    calls = 0

    def reconcile(_service, candidate_run_id: str):
        nonlocal calls
        assert candidate_run_id == "candidate-1"
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "app.services.backtest.strategy_experiment_service.StrategyExperimentService.reconcile_candidate",
        reconcile,
    )

    with pytest.raises(sqlite3.OperationalError):
        service._reconcile_pending_experiments()
    service._reconcile_pending_experiments()

    assert calls == 2
