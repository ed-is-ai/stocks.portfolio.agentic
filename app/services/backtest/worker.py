"""Claimed Strategy Manager worker module entry point."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Protocol

from app.core import config
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.historical_initialization_engine import (
    CanonicalSnapshotMonthProcessor,
    HistoricalInitializationEngine,
)
from app.services.backtest.reconstruction_roster import CapturedRosterV1
from app.services.backtest.strategy_job import (
    JobFailureCode,
    StrategyJobStatus,
    StrategyJobType,
)


class WorkerResult(Protocol):
    @property
    def status(self) -> StrategyJobStatus: ...


class WorkerEngine(Protocol):
    def run(self, job_id: str, claim_token: str) -> WorkerResult: ...


def build_worker_repository() -> BacktestRepository:
    repository = BacktestRepository(db.make_connect(lambda: str(config.BACKTEST_DB)))
    repository.ensure_schema()
    return repository


def build_initialization_engine(
    job_id: str, claim_token: str, backtest: BacktestRepository
) -> HistoricalInitializationEngine:
    """Build independent schema-ready repositories for one claimed child."""
    prices = HistoricalPriceRepository(
        db.make_connect(lambda: str(config.HISTORICAL_PRICE_CACHE))
    )
    prices.ensure_schema()
    initialization = backtest.initialization_run(job_id)
    profile = backtest.snapshot_profile(initialization.profile_hash)
    if profile is None:
        raise RuntimeError("Pinned snapshot profile is unavailable")
    roster_json = backtest.roster_manifest_json(profile.roster_digest)
    if roster_json is None:
        raise RuntimeError("Pinned reconstruction roster is unavailable")
    roster = CapturedRosterV1.from_json(profile.roster_digest, roster_json)
    processor = CanonicalSnapshotMonthProcessor(
        job_id=job_id,
        claim_token=claim_token,
        profile=profile,
        roster=roster,
        backtest_repository=backtest,
        price_repository=prices,
    )

    def qualified() -> bool:
        return (
            backtest.current_qualification_contract_digest()
            == initialization.qualification_contract_digest
        )

    def profile_is_current(profile_hash: str) -> bool:
        try:
            current = backtest.snapshot_profile(profile_hash)
            return (
                current is not None
                and current.calendar_dataset_version
                == initialization.calendar_dataset_version
            )
        except Exception:
            return False

    return HistoricalInitializationEngine(
        backtest,
        processor,
        qualification_check=qualified,
        profile_check=profile_is_current,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    engine_factory: Callable[[str], WorkerEngine] | None = None,
    repository_factory: Callable[[], BacktestRepository] = build_worker_repository,
) -> int:
    parser = argparse.ArgumentParser(description="Run one claimed Strategy job")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--claim-token", required=True)
    args = parser.parse_args(argv)
    if engine_factory is not None:
        engine = engine_factory(args.job_id)
        result = engine.run(args.job_id, args.claim_token)
        return (
            0
            if result.status
            in {StrategyJobStatus.COMPLETE, StrategyJobStatus.CANCELLED}
            else 1
        )

    repository = repository_factory()
    job = repository.strategy_job(args.job_id)
    if (
        job.status is not StrategyJobStatus.RUNNING
        or job.claim_token != args.claim_token
    ):
        return 1
    try:
        if job.job_type is not StrategyJobType.INITIALIZATION:
            raise RuntimeError("Unsupported Strategy job type")
        engine = build_initialization_engine(args.job_id, args.claim_token, repository)
    except Exception:
        current = repository.strategy_job(args.job_id)
        if (
            current.status is StrategyJobStatus.RUNNING
            and current.claim_token == args.claim_token
        ):
            if current.cancel_requested_at is not None:
                repository.cancel_claimed_strategy_job(
                    current.id,
                    args.claim_token,
                    expected_version=current.status_version,
                )
            else:
                repository.fail_claimed_strategy_job(
                    current.id,
                    args.claim_token,
                    expected_version=current.status_version,
                    failure_code=JobFailureCode.INTEGRITY_ERROR,
                    failed_month=None,
                    detail="Strategy worker configuration is invalid",
                )
        return 1
    result = engine.run(args.job_id, args.claim_token)
    return (
        0
        if result.status
        in {
            StrategyJobStatus.COMPLETE,
            StrategyJobStatus.CANCELLED,
        }
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
