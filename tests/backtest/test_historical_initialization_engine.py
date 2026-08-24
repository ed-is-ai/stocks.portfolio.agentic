from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import cast

import pytest

from app.services.backtest.historical_initialization_engine import (
    CanonicalSnapshotMonthProcessor,
    HistoricalInitializationEngine,
    InitializationMonthError,
    InitializationRepository,
)
from app.services.backtest.historical_price_evidence import HistoricalEvidenceRequest
from app.services.backtest.reconstruction_roster import (
    CapturedRosterMemberV1,
    CapturedRosterV1,
)
from app.services.backtest.strategy_job import (
    JobFailureCode,
    StrategyJobStatus,
    WorkerLeaseFenceV1,
)
from app.services.backtest.strategy_job import StrategyJobConflict
from app.services.backtest.trading_calendar import CalendarContractError


class Status(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str = "job-1"
    job_type: str = "initialization"
    status: object = StrategyJobStatus.RUNNING
    claim_token: str | None = "claim-1"
    status_version: int = 2
    cancel_requested_at: object | None = None


@dataclass
class Initialization:
    job_id: str = "job-1"
    profile_hash: str = "a" * 64
    requested_start: str = "2026-05"
    requested_end: str = "2026-07"
    requested_months: tuple[str, ...] = ("2026-05", "2026-06", "2026-07")
    qualification_contract_digest: str = "b" * 64


class FakeRepository:
    def __init__(self, *, ready: set[str] | None = None) -> None:
        self.job = Job()
        self.initialization = Initialization()
        self.ready = ready or set()
        self.progress: list[str] = []
        self.failed: list[dict[str, object]] = []
        self.cancelled = False
        self.completed = False
        self.leases: list[WorkerLeaseFenceV1 | None] = []

    def strategy_job(self, _job_id):
        return self.job

    def initialization_run(self, _job_id):
        return self.initialization

    def interval_readiness(self, _profile, start, _end):
        @dataclass
        class Ready:
            ready: bool

        return Ready(start in self.ready)

    def set_strategy_job_current_month(
        self, _job_id, _token, *, expected_version, month, lease=None
    ):
        self.leases.append(lease)
        assert expected_version == self.job.status_version
        self.progress.append(month)
        self.job = replace(self.job, status_version=expected_version + 1)
        return self.job

    def fail_claimed_strategy_job(self, _job_id, _token, **kwargs):
        self.failed.append(kwargs)
        self.job = replace(self.job, status=StrategyJobStatus.FAILED)
        return self.job

    def cancel_claimed_strategy_job(
        self, _job_id, _token, *, expected_version, lease=None
    ):
        self.leases.append(lease)
        self.cancelled = True
        self.job = replace(self.job, status=StrategyJobStatus.CANCELLED)
        return self.job

    def complete_claimed_initialization_job(
        self, _job_id, _token, *, expected_version, lease=None
    ):
        self.leases.append(lease)
        assert expected_version == self.job.status_version
        self.completed = True
        self.job = replace(self.job, status=StrategyJobStatus.COMPLETE)
        return self.job


def test_engine_processes_months_ascending_and_reuses_ready_months() -> None:
    repo = FakeRepository(ready={"2026-05"})
    processed: list[str] = []
    engine = HistoricalInitializationEngine(
        cast(InitializationRepository, repo), processed.append
    )

    result = engine.run("job-1", "claim-1")

    assert repo.progress == ["2026-05", "2026-06", "2026-07"]
    assert processed == ["2026-06", "2026-07"]
    assert result.status is StrategyJobStatus.COMPLETE


def test_engine_propagates_worker_lease_to_lifecycle_writes() -> None:
    repo = FakeRepository(ready=set(Initialization().requested_months))
    lease = WorkerLeaseFenceV1(instance_id="worker-1", generation=7)

    result = HistoricalInitializationEngine(
        cast(InitializationRepository, repo),
        lambda _month: None,
        lease=lease,
    ).run("job-1", "claim-1")

    assert result.status is StrategyJobStatus.COMPLETE
    assert repo.leases == [lease, lease, lease, lease]


def test_bats_uses_new_york_exchange_timezone() -> None:
    assert CanonicalSnapshotMonthProcessor._TIMEZONES["BATS"] == "America/New_York"


def test_first_failure_stops_later_months_and_records_safe_failure() -> None:
    repo = FakeRepository()
    processed: list[str] = []

    def process(month: str) -> None:
        assert repo.progress[-1] == month
        processed.append(month)
        if month == "2026-06":
            raise InitializationMonthError(
                JobFailureCode.REQUIRED_DATA_MISSING,
                "Required historical data is unavailable",
            )

    engine = HistoricalInitializationEngine(
        cast(InitializationRepository, repo), process
    )

    result = engine.run("job-1", "claim-1")

    assert result.status is StrategyJobStatus.FAILED
    assert processed == ["2026-05", "2026-06"]
    assert repo.failed == [
        {
            "expected_version": 4,
            "failure_code": JobFailureCode.REQUIRED_DATA_MISSING,
            "failed_month": "2026-06",
            "detail": "Required historical data is unavailable",
            "lease": None,
        }
    ]


def test_cancellation_is_honoured_before_next_month_boundary() -> None:
    repo = FakeRepository()

    def process(month: str) -> None:
        if month == "2026-05":
            repo.job = replace(
                repo.job,
                cancel_requested_at=object(),
                status_version=repo.job.status_version + 1,
            )

    result = HistoricalInitializationEngine(
        cast(InitializationRepository, repo), process
    ).run("job-1", "claim-1")

    assert result.status is StrategyJobStatus.CANCELLED
    assert repo.progress == ["2026-05"]
    assert repo.cancelled is True


def test_preexisting_cancel_intent_stops_before_first_month() -> None:
    repo = FakeRepository()
    repo.job = replace(
        repo.job,
        cancel_requested_at=object(),
        status_version=repo.job.status_version + 1,
    )
    processed: list[str] = []

    result = HistoricalInitializationEngine(
        cast(InitializationRepository, repo), processed.append
    ).run("job-1", "claim-1")

    assert result.status is StrategyJobStatus.CANCELLED
    assert processed == []
    assert repo.progress == []


def test_wrong_claim_is_rejected_without_processing() -> None:
    repo = FakeRepository()
    processed: list[str] = []

    result = HistoricalInitializationEngine(
        cast(InitializationRepository, repo), processed.append
    ).run("job-1", "stale-claim")

    assert result.status is StrategyJobStatus.RUNNING
    assert processed == []
    assert repo.failed == []


def test_member_processing_is_stable_and_stops_at_first_failure() -> None:
    def member(security_id: str) -> CapturedRosterMemberV1:
        return CapturedRosterMemberV1(
            security_id=security_id,
            mic="XNAS",
            calendar="XNYS",
            provider_symbol=security_id.upper(),
            currency="USD",
            quote_unit="USD",
            source_memberships=(),
            identity_evidence=(),
            evidence_digest="a" * 64,
        )

    processor = object.__new__(CanonicalSnapshotMonthProcessor)
    setattr(
        processor,
        "_roster",
        CapturedRosterV1("a" * 64, "{}", (member("z"), member("b"), member("a"))),
    )
    setattr(processor, "_clock", lambda: datetime(2026, 8, 12, tzinfo=timezone.utc))
    setattr(
        processor,
        "_calendar",
        SimpleNamespace(month_sessions=lambda *_a, **_k: {"XNAS": date(2026, 7, 31)}),
    )
    visited: list[str] = []

    def resolve(_self, roster_member, *_args):
        visited.append(roster_member.security_id)
        if roster_member.security_id == "b":
            raise InitializationMonthError(
                JobFailureCode.REQUIRED_DATA_MISSING,
                "Required historical data is unavailable",
            )
        return SimpleNamespace(member=None, record=None)

    setattr(processor, "_resolve_member", MethodType(resolve, processor))

    with pytest.raises(InitializationMonthError):
        processor("2026-07")

    assert visited == ["a", "b"]


def test_cached_evidence_is_reused_without_provider_access() -> None:
    cached = SimpleNamespace(
        security_id="security-1",
        alias_revision="b" * 64,
        requested_symbol="AAPL",
        observed_symbol="AAPL",
        currency="USD",
        quote_unit="USD",
        exchange_timezone="America/New_York",
        rows=({"session": "2026-07-31"},),
    )

    class Prices:
        def find_request(self, **_kwargs):
            return cached

    class Provider:
        def fetch(self, _request):
            raise AssertionError("cached evidence must prevent provider access")

    processor = object.__new__(CanonicalSnapshotMonthProcessor)
    setattr(processor, "_price_repository", Prices())
    setattr(processor, "_evidence_adapter", Provider())
    setattr(processor, "_alias_revision", "b" * 64)
    roster_member = SimpleNamespace(security_id="security-1", provider_symbol="AAPL")
    request = HistoricalEvidenceRequest(
        security_id="security-1",
        alias_revision="b" * 64,
        symbol="AAPL",
        start=date(1970, 1, 1),
        end=date(2026, 8, 1),
        expected_currency="USD",
        expected_quote_unit="USD",
        expected_timezone="America/New_York",
        expected_sessions=(date(2026, 7, 31),),
        allowed_observed_symbols=("AAPL",),
        allow_missing_prefix=True,
    )

    assert processor._evidence_for(roster_member, request) is cached


def test_calendar_contract_failure_keeps_calendar_failure_code() -> None:
    processor = object.__new__(CanonicalSnapshotMonthProcessor)
    setattr(processor, "_clock", lambda: datetime(2026, 8, 12, tzinfo=timezone.utc))
    setattr(processor, "_roster", SimpleNamespace(members=()))
    setattr(
        processor,
        "_calendar",
        SimpleNamespace(
            month_sessions=lambda *_a, **_k: (_ for _ in ()).throw(
                CalendarContractError("bad calendar")
            )
        ),
    )

    with pytest.raises(InitializationMonthError) as error:
        processor("2026-07")

    assert error.value.code is JobFailureCode.CALENDAR_ERROR


def test_cancel_intent_wins_version_race_at_failure_boundary() -> None:
    class RacingRepository(FakeRepository):
        def fail_claimed_strategy_job(self, _job_id, _token, **kwargs):
            self.job = replace(
                self.job,
                cancel_requested_at=object(),
                status_version=self.job.status_version + 1,
            )
            raise StrategyJobConflict("cancellation won")

    repo = RacingRepository()

    def process(_month: str) -> None:
        raise InitializationMonthError(
            JobFailureCode.REQUIRED_DATA_MISSING,
            "Required historical data is unavailable",
        )

    result = HistoricalInitializationEngine(
        cast(InitializationRepository, repo), process
    ).run("job-1", "claim-1")

    assert result.status is StrategyJobStatus.CANCELLED
    assert repo.cancelled is True


def test_strategy_lifecycle_modules_do_not_import_live_trading_authority() -> None:
    root = Path(__file__).resolve().parents[2]
    forbidden = (
        "app.agents",
        "TraderAgent",
        "trades_repo",
        "portfolio_repo",
        "cash_repo",
        "order_submission",
        "notifications_repo",
    )
    for relative in (
        "app/services/backtest/strategy_job.py",
        "app/services/backtest/strategy_job_service.py",
        "app/services/backtest/historical_initialization_engine.py",
        "app/services/backtest/worker.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert all(token not in source for token in forbidden)
