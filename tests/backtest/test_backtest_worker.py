from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import sqlite3

import pandas as pd
import pytest

from app.core import config
from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.run_input_manifest import (
    ENGINE_VERSION,
    PROTOCOL_SCHEMA_VERSION,
    DetectorSourceDigestV1,
    PinnedSecurityEvidenceV1,
    RunInputManifestV1,
)
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    StrategyUniverseContractV1,
)
from app.services.backtest.snapshot_profile import (
    IntervalReadinessV1,
    ProfileDetectorV1,
    SnapshotProfileV1,
)
from app.services.backtest.source_manifest import detector_source_manifests
from app.services.backtest.strategy_job import (
    BacktestSubmissionV1,
    JobFailureCode,
    STAGE_SEQUENCES,
    StrategyJobStatus,
    StrategyJobType,
)
from app.services.backtest.trading_calendar import TradingCalendar
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


def test_backtest_engine_construction_failure_is_not_mislabeled_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the initialization construction-failure test above -- now
    that real dispatch exists for backtest (unlike the old ``main()``
    that unconditionally rejected every backtest job), a genuinely broken
    ``build_backtest_engine`` must map to the same generic, safe
    integrity failure, never a stranded/mislabeled interruption."""
    repo = Repository(ClaimedJob(job_type=StrategyJobType.BACKTEST))

    def broken(*_args, **_kwargs):
        raise RuntimeError("corrupt strategy run")

    monkeypatch.setattr(worker_module, "build_backtest_engine", broken)

    assert (
        main(
            ["--job-id", "job-1", "--claim-token", "claim-1"],
            repository_factory=lambda: repo,  # type: ignore[arg-type]
        )
        == 1
    )
    assert repo.failures[0]["failure_code"] is JobFailureCode.INTEGRITY_ERROR
    assert repo.failures[0]["detail"] == "Strategy worker configuration is invalid"


# ---------------------------------------------------------------------------
# Story 2.6: real end-to-end Backtest execution -- claim through completion/
# cancellation/fatal-failure, driving the real repository, engine, and
# staging sink together (not a fake repository double).
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
ROSTER_DIGEST = "b" * 64
SECURITY_ID = "sec-001"
FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "backtest-strategies"
STRATEGY_SOURCE_DIGEST = "7" * 64
ORDERED_MONTH_DIGEST = "6" * 64


def _authoritative_detectors() -> tuple[ProfileDetectorV1, ...]:
    project_root = Path(__file__).resolve().parents[2]
    manifests = detector_source_manifests(project_root)
    return tuple(
        ProfileDetectorV1(
            detector_id=detector.detector_id,
            detector_api_version=detector.detector_api_version,
            detector_version=manifests[detector.detector_id].digest,
        )
        for detector in DETECTOR_REGISTRY
    )


def _profile() -> SnapshotProfileV1:
    return SnapshotProfileV1(
        schema_version="snapshot_profile.v1",
        display_version="Scanner data v1",
        record_schema_version="historical_scan_record.v1",
        detectors=_authoritative_detectors(),
        roster_policy_version="ReconstructionRosterPolicyV1",
        roster_digest=ROSTER_DIGEST,
        identity_registry_version="SecurityIdentityRegistryV1",
        alias_policy_version="SecurityAliasManifestV1",
        source_policy_version="FreeHistoricalSourcePolicyV1",
        calendar_policy_version="PerExchangeMonthEndV1",
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=TradingCalendar().session_table_digest(),
        yfinance_request_contract_version="yfinance-daily-v1",
        yfinance_ingestion_version="ingestion-v1",
        market_plane_policy_version="HistoricalMarketPlanesV1",
        reconstructability_policy_version="reconstructability.v1",
        provenance_vocabulary=("best_effort_reconstructed", "observed_bau"),
        cadence="per-exchange month_end",
    )


#: Deterministic (content-derived) -- computed once so every test can pass
#: the exact ``profile_hash`` the committed profile actually has.
PROFILE_HASH = _profile().profile_hash


def _repo(path: Path) -> BacktestRepository:
    repo = BacktestRepository(
        db.make_connect(lambda: path),
        clock=lambda: date(2026, 6, 1),
        instant_clock=lambda: NOW,
    )
    repo.ensure_schema()
    profile = _profile()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT OR IGNORE INTO security_identity_registry_revisions
               (revision_digest, canonical_manifest_json, evidence_digest, created_at)
               VALUES (?, '{}', ?, ?)""",
            ("c" * 64, "d" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO security_alias_manifests
               (alias_revision, canonical_manifest_json, evidence_digest, created_at)
               VALUES (?, '{}', ?, ?)""",
            ("e" * 64, "f" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO reconstruction_rosters
               (roster_digest, policy_version, canonical_manifest_json,
                identity_registry_revision, alias_revision, captured_at)
               VALUES (?, 'ReconstructionRosterPolicyV1', '{}', ?, ?, ?)""",
            (ROSTER_DIGEST, "c" * 64, "e" * 64, NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO snapshot_profiles
               (profile_hash, canonical_profile_json, display_version, roster_digest,
                scanner_schema_version, calendar_dataset_version,
                calendar_dataset_digest, cadence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile.profile_hash,
                profile.canonical_json(),
                profile.display_version,
                profile.roster_digest,
                profile.record_schema_version,
                profile.calendar_dataset_version,
                profile.calendar_dataset_digest,
                profile.cadence,
            ),
        )
        conn.execute(
            """INSERT OR IGNORE INTO active_snapshot_profile
               (singleton_id, profile_hash, activation_seq, activated_at)
               VALUES (1, ?, 1, ?)""",
            (PROFILE_HASH, NOW.isoformat()),
        )
    return repo


def _patch_ready(repo: BacktestRepository, digest: str = ORDERED_MONTH_DIGEST) -> None:
    def _readiness(_conn, profile_hash, start_month, end_month):  # noqa: ANN001
        return IntervalReadinessV1(
            profile_hash=profile_hash,
            start_month=start_month,
            end_month=end_month,
            ready=True,
            no_op=True,
            missing_months=(),
            ordered_month_digest=digest,
        )

    repo._interval_readiness_on_connection = _readiness  # type: ignore[method-assign]


class _FakeTicker:
    def __init__(self, frame: pd.DataFrame, symbol: str) -> None:
        self._frame = frame
        self._symbol = symbol

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {
            "symbol": self._symbol,
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
        }


def _price_repo(tmp_path: Path) -> HistoricalPriceRepository:
    repo = HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "prices.db"))
    repo.ensure_schema()
    return repo


def _commit_evidence(
    prices: HistoricalPriceRepository,
    *,
    security_id: str,
    sessions: tuple[date, ...],
) -> str:
    frame = pd.DataFrame(
        {
            "Open": [100.0 for _ in sessions],
            "High": [101.0 for _ in sessions],
            "Low": [99.0 for _ in sessions],
            "Close": [100.5 for _ in sessions],
            "Adj Close": [100.5 for _ in sessions],
            "Volume": [1_000.0 for _ in sessions],
            "Dividends": [0.0 for _ in sessions],
            "Stock Splits": [0.0 for _ in sessions],
        },
        index=pd.DatetimeIndex(
            [session.isoformat() for session in sessions], tz="America/New_York"
        ),
    )
    request = HistoricalEvidenceRequest(
        security_id=security_id,
        alias_revision="4" * 64,
        symbol=security_id,
        start=sessions[0],
        end=sessions[-1] + timedelta(days=1),
        expected_currency="USD",
        expected_quote_unit="USD",
        expected_timezone="America/New_York",
        expected_sessions=sessions,
        allowed_observed_symbols=(security_id,),
    )
    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: _FakeTicker(frame, security_id), clock=lambda: NOW
    ).fetch(request)
    prices.commit(payload)
    return payload.data_revision


def _detector_digests() -> tuple[DetectorSourceDigestV1, ...]:
    return tuple(
        DetectorSourceDigestV1(detector_id=detector.detector_id, source_digest="a" * 64)
        for detector in DETECTOR_REGISTRY
    )


def _manifest(
    *,
    revision: str,
    start_month: str,
    end_month: str,
    parameters: dict[str, object] | None = None,
    starting_capital: Decimal = Decimal("10000"),
) -> RunInputManifestV1:
    return RunInputManifestV1(
        schema_version="run_input_manifest.v1",
        engine_version=ENGINE_VERSION,
        protocol_schema_version=PROTOCOL_SCHEMA_VERSION,
        market_view_source_digest="1" * 64,
        ledger_action_metrics_digest="2" * 64,
        numeric_rounding_policy="HistoricalMarketPlanesV1",
        runtime_lock_digest="3" * 64,
        calendar_session_table_digest=TradingCalendar().session_table_digest(),
        python_runtime="3.13",
        timezone_dataset_version="2026.2",
        strategy_id="momentum_v1",
        strategy_api_version=1,
        strategy_source_digest=STRATEGY_SOURCE_DIGEST,
        detector_source_digests=_detector_digests(),
        parameters=parameters or {},
        alias_revision="4" * 64,
        securities=(
            PinnedSecurityEvidenceV1(
                security_id=SECURITY_ID,
                price_revision=revision,
                action_revision=revision,
                fx_revision=None,
            ),
        ),
        profile_hash=PROFILE_HASH,
        start_month=start_month,
        end_month=end_month,
        ordered_month_digest=ORDERED_MONTH_DIGEST,
        base_currency="USD",
        starting_capital=starting_capital,
    )


def _enqueue(repo: BacktestRepository, manifest: RunInputManifestV1):
    _patch_ready(repo)
    return repo.create_backtest_job(
        BacktestSubmissionV1(
            strategy_id=manifest.strategy_id,
            strategy_api_version=manifest.strategy_api_version,
            strategy_source_digest=manifest.strategy_source_digest,
            parameters=dict(manifest.parameters),
            profile_hash=manifest.profile_hash,
            start_month=manifest.start_month,
            end_month=manifest.end_month,
            base_currency=manifest.base_currency,
            starting_capital=manifest.starting_capital,
            run_input_manifest_digest=manifest.digest(),
            execution_contract_digest=manifest.execution_contract_digest(),
            canonical_manifest_json=manifest.canonical_json(),
        )
    )


def _fixture_universe() -> StrategyUniverseContractV1:
    return StrategyUniverseContractV1(
        schema_version="strategy_universe.v1",
        mode="selected-securities",
        parameter="selected_securities",
    )


def _fake_descriptor() -> StrategyDescriptorV1:
    return StrategyDescriptorV1(
        strategy_id="momentum_v1",
        source_manifest_version="strategy_source_manifest.v1",
        source_digest=STRATEGY_SOURCE_DIGEST,
        display_name="Momentum",
        description="Test fixture strategy",
        api_version=1,
        parameters=(),
        default_parameters={},
        runtime_path="minimal-strategy/scripts/strategy.py",
        runtime_files=("minimal-strategy/scripts/strategy.py",),
        universe=_fixture_universe(),
    )


def _patch_strategy_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass real filesystem Skill discovery (already Story 2.2's own
    coverage) while still exercising the worker's own real dynamic-import
    loader against the real ``minimal-strategy`` fixture module."""
    monkeypatch.setattr(
        worker_module,
        "_resolve_strategy_descriptor",
        lambda _strategy_id: _fake_descriptor(),
    )
    monkeypatch.setattr(config, "SKILLS_DIR", FIXTURES_ROOT)


def test_worker_completes_a_real_backtest_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_strategy_resolution(monkeypatch)
    repo = _repo(tmp_path / "backtest.db")
    prices = _price_repo(tmp_path)
    sessions = TradingCalendar().sessions_in_range(
        "XNYS", date(2026, 6, 1), date(2026, 7, 1)
    )
    revision = _commit_evidence(prices, security_id=SECURITY_ID, sessions=sessions)
    manifest = _manifest(
        revision=revision,
        start_month="2026-06",
        end_month="2026-06",
        parameters={"watch_security_id": SECURITY_ID, "fixed_shares": 1},
    )
    enqueued = _enqueue(repo, manifest)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    engine = worker_module.build_backtest_engine(claim.job.id, claim.claim_token, repo)
    engine._prices = prices  # type: ignore[attr-defined]

    result = engine.run(claim.job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.COMPLETE
    backtest_result = repo.backtest_result(enqueued.job.id)
    assert len(backtest_result.events) > 0
    assert len(backtest_result.equity_curve) == len(sessions)
    with sqlite3.connect(tmp_path / "backtest.db") as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (enqueued.job.id,)
            ).fetchone()
            is None
        )


def test_worker_honours_a_cancellation_requested_mid_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``write_backtest_staging``'s own CAS predicate (Story 2.5) rejects
    any staging write the instant ``cancel_requested_at`` is set --
    before the next month-boundary check would otherwise notice it. The
    worker must recognize that specific conflict as "cancellation, not
    ownership loss" and still reach the atomic cancel transition, rather
    than silently returning a stranded ``running`` job."""
    _patch_strategy_resolution(monkeypatch)
    repo = _repo(tmp_path / "backtest.db")
    prices = _price_repo(tmp_path)
    sessions = TradingCalendar().sessions_in_range(
        "XNYS", date(2026, 6, 1), date(2026, 8, 1)
    )
    revision = _commit_evidence(prices, security_id=SECURITY_ID, sessions=sessions)
    manifest = _manifest(
        revision=revision,
        start_month="2026-06",
        end_month="2026-07",
        parameters={"watch_security_id": SECURITY_ID, "fixed_shares": 1},
    )
    enqueued = _enqueue(repo, manifest)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    real_progress = BacktestRepository.set_strategy_job_current_month

    def request_cancel_after_first_month(
        self, job_id, claim_token, *, expected_version, month
    ):  # noqa: ANN001
        job = real_progress(
            self, job_id, claim_token, expected_version=expected_version, month=month
        )
        if month == "2026-06":
            self.request_strategy_job_cancellation(
                job_id, expected_version=job.status_version
            )
        return job

    monkeypatch.setattr(
        BacktestRepository,
        "set_strategy_job_current_month",
        request_cancel_after_first_month,
    )

    engine = worker_module.build_backtest_engine(claim.job.id, claim.claim_token, repo)
    engine._prices = prices  # type: ignore[attr-defined]

    result = engine.run(claim.job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.CANCELLED
    with pytest.raises(Exception):
        repo.backtest_result(enqueued.job.id)
    with sqlite3.connect(tmp_path / "backtest.db") as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (enqueued.job.id,)
            ).fetchone()
            is None
        )


def test_worker_honours_a_cancellation_pending_before_the_first_month_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clean, literal AC 5 case: cancellation is already recorded
    before the worker begins, so ``_ProgressObserver`` itself refuses the
    very first month boundary -- no staging is ever written, no later
    month is ever published."""
    _patch_strategy_resolution(monkeypatch)
    repo = _repo(tmp_path / "backtest.db")
    prices = _price_repo(tmp_path)
    sessions = TradingCalendar().sessions_in_range(
        "XNYS", date(2026, 6, 1), date(2026, 7, 1)
    )
    revision = _commit_evidence(prices, security_id=SECURITY_ID, sessions=sessions)
    manifest = _manifest(
        revision=revision,
        start_month="2026-06",
        end_month="2026-06",
        parameters={"watch_security_id": SECURITY_ID, "fixed_shares": 1},
    )
    enqueued = _enqueue(repo, manifest)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    repo.request_strategy_job_cancellation(
        claim.job.id, expected_version=claim.job.status_version
    )

    engine = worker_module.build_backtest_engine(claim.job.id, claim.claim_token, repo)
    engine._prices = prices  # type: ignore[attr-defined]

    result = engine.run(claim.job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.CANCELLED
    assert result.current_month is None
    with sqlite3.connect(tmp_path / "backtest.db") as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (enqueued.job.id,)
            ).fetchone()
            is None
        )


def test_worker_maps_missing_pinned_evidence_to_required_data_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_strategy_resolution(monkeypatch)
    repo = _repo(tmp_path / "backtest.db")
    prices = _price_repo(tmp_path)
    manifest = _manifest(
        revision="9" * 64,  # never committed
        start_month="2026-06",
        end_month="2026-06",
        parameters={"watch_security_id": SECURITY_ID, "fixed_shares": 1},
    )
    _enqueue(repo, manifest)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    engine = worker_module.build_backtest_engine(claim.job.id, claim.claim_token, repo)
    engine._prices = prices  # type: ignore[attr-defined]

    result = engine.run(claim.job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.REQUIRED_DATA_MISSING
    with sqlite3.connect(tmp_path / "backtest.db") as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM backtest_staging WHERE run_id=?", (claim.job.id,)
            ).fetchone()
            is None
        )


def test_worker_maps_a_fatal_missing_open_to_required_data_missing_with_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commits evidence for only the first two sessions of the month, so
    the third session's scheduled SELL fill has no as-traded open --
    ``SimulationErrorCode.MISSING_REQUIRED_OPEN`` -- proving a genuine
    fatal engine error maps to the documented failure code with its
    deterministic failed month, staging discarded, never a stray
    ``complete``."""
    _patch_strategy_resolution(monkeypatch)
    repo = _repo(tmp_path / "backtest.db")
    prices = _price_repo(tmp_path)
    full_sessions = TradingCalendar().sessions_in_range(
        "XNYS", date(2026, 6, 1), date(2026, 7, 1)
    )
    revision = _commit_evidence(
        prices, security_id=SECURITY_ID, sessions=full_sessions[:2]
    )
    manifest = _manifest(
        revision=revision,
        start_month="2026-06",
        end_month="2026-06",
        parameters={"watch_security_id": SECURITY_ID, "fixed_shares": 1},
    )
    _enqueue(repo, manifest)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    engine = worker_module.build_backtest_engine(claim.job.id, claim.claim_token, repo)
    engine._prices = prices  # type: ignore[attr-defined]

    result = engine.run(claim.job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.REQUIRED_DATA_MISSING
    assert result.failed_month == "2026-06"


def test_worker_maps_strategy_identity_mismatch_to_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        worker_module,
        "_resolve_strategy_descriptor",
        lambda _strategy_id: StrategyDescriptorV1(
            strategy_id="momentum_v1",
            source_manifest_version="strategy_source_manifest.v1",
            source_digest="9" * 64,  # deliberately mismatched
            display_name="Momentum",
            description="Test fixture strategy",
            api_version=1,
            parameters=(),
            default_parameters={},
            runtime_path="minimal-strategy/scripts/strategy.py",
            runtime_files=("minimal-strategy/scripts/strategy.py",),
            universe=_fixture_universe(),
        ),
    )
    monkeypatch.setattr(config, "SKILLS_DIR", FIXTURES_ROOT)
    repo = _repo(tmp_path / "backtest.db")
    prices = _price_repo(tmp_path)
    manifest = _manifest(
        revision="9" * 64,
        start_month="2026-06",
        end_month="2026-06",
    )
    _enqueue(repo, manifest)
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    engine = worker_module.build_backtest_engine(claim.job.id, claim.claim_token, repo)
    engine._prices = prices  # type: ignore[attr-defined]

    result = engine.run(claim.job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.INTEGRITY_ERROR
    assert result.failure_detail is not None
    assert "no longer matches" in result.failure_detail


def test_safe_detail_falls_back_when_code_and_message_are_both_empty() -> None:
    """``f"{code}: {message}".strip()`` always contains the literal ``":"``
    even when both inputs are empty, so the fallback text is only ever
    reachable when the emptiness check tests ``code``/``message``
    directly rather than the already-joined string."""
    assert worker_module._safe_detail("", "") == "backtest simulation failed"


def test_build_backtest_engine_wires_a_real_historical_price_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every end-to-end test above immediately overwrites the private
    ``engine._prices`` with a fake, so ``build_backtest_engine``'s real
    wiring of ``HistoricalPriceRepository`` against
    ``config.HISTORICAL_PRICE_CACHE`` is never otherwise exercised. This
    test proves that wiring alone -- no simulation required."""
    price_cache_path = tmp_path / "historical_price_cache.db"
    monkeypatch.setattr(config, "HISTORICAL_PRICE_CACHE", price_cache_path)
    repo = _repo(tmp_path / "backtest.db")
    manifest = _manifest(revision="5" * 64, start_month="2026-06", end_month="2026-06")
    enqueued = _enqueue(repo, manifest)

    engine = worker_module.build_backtest_engine(
        enqueued.job.id, "unused-claim-token", repo
    )

    assert isinstance(engine._prices, HistoricalPriceRepository)
    with sqlite3.connect(price_cache_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "historical_price_revisions" in tables


def test_progress_observer_raises_engine_defect_for_out_of_range_month(
    tmp_path: Path,
) -> None:
    """``on_month_boundary`` must distinguish a genuine engine defect (the
    engine reports a month outside the Strategy Run's own pinned range)
    from a legitimate CAS ownership race -- and must never reach the
    repository's own CAS write for the defect case."""
    repo = _repo(tmp_path / "backtest.db")
    manifest = _manifest(revision="5" * 64, start_month="2026-06", end_month="2026-06")
    _enqueue(repo, manifest)
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    assert claim.backtest is not None
    state = worker_module._ClaimState(
        job_id=claim.job.id,
        claim_token=claim.claim_token,
        status_version=claim.job.status_version,
    )
    observer = worker_module._ProgressObserver(
        repository=repo, state=state, backtest=claim.backtest
    )

    with pytest.raises(worker_module._BacktestEngineDefect):
        observer.on_month_boundary(month="2099-01")

    assert repo.strategy_job(claim.job.id).current_month is None


# ---------------------------------------------------------------------------
# Story 4.1: stage-walking Bootstrap/Preparation stub workers.
# ---------------------------------------------------------------------------


def _stage_repo(path: Path) -> BacktestRepository:
    repo = BacktestRepository(
        db.make_connect(lambda: path),
        clock=lambda: date(2026, 8, 21),
        instant_clock=lambda: datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc),
    )
    repo.ensure_schema()
    _seed_bootstrap_prerequisites(path)
    return repo


_STAGE_NOW = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)
_STAGE_PROFILE_HASH = "a" * 64
_STAGE_ROSTER_DIGEST = "b" * 64
_STAGE_FIXTURE_DIGEST = "1" * 64
_STAGE_PROBE_DEFINITION_DIGEST = "2" * 64


def _stage_qualification_digest() -> str:
    from app.services.backtest.canonical_manifest import manifest_digest
    from app.services.backtest.historical_data_qualification import (
        FIXTURE_CONTRACT_VERSION,
        REQUEST_CONTRACT_VERSION,
        current_source_versions_json,
    )

    return manifest_digest(
        {
            "sources": json.loads(current_source_versions_json()),
            "calendar_digest": TradingCalendar().session_table_digest(),
            "request_contract": REQUEST_CONTRACT_VERSION,
            "fixture_contract": FIXTURE_CONTRACT_VERSION,
            "fixture_digest": _STAGE_FIXTURE_DIGEST,
            "probe_definition_digest": _STAGE_PROBE_DEFINITION_DIGEST,
        }
    )


def _seed_bootstrap_prerequisites(path: Path) -> None:
    """Seed qualification, roster, identities, and active profile."""
    from app.services.backtest.historical_data_qualification import (
        current_source_versions_json,
    )

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT OR IGNORE INTO security_identity_registry_revisions
               (revision_digest, canonical_manifest_json, evidence_digest, created_at)
               VALUES (?, '{}', ?, ?)""",
            ("c" * 64, "d" * 64, _STAGE_NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO security_alias_manifests
               (alias_revision, canonical_manifest_json, evidence_digest, created_at)
               VALUES (?, '{}', ?, ?)""",
            ("e" * 64, "f" * 64, _STAGE_NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO reconstruction_rosters
               (roster_digest, policy_version, canonical_manifest_json,
                identity_registry_revision, alias_revision, captured_at)
               VALUES (?, 'ReconstructionRosterPolicyV1', '{}', ?, ?, ?)""",
            (
                _STAGE_ROSTER_DIGEST,
                "c" * 64,
                "e" * 64,
                _STAGE_NOW.isoformat(),
            ),
        )
        conn.execute(
            """INSERT OR IGNORE INTO security_identities
               (security_id, mic, provider_symbol, evidence_digest,
                identity_registry_revision, created_at)
               VALUES (?, 'XNYS', 'TEST', ?, ?, ?)""",
            ("sid_test_001", "g" * 64, "c" * 64, _STAGE_NOW.isoformat()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO snapshot_profiles
               (profile_hash, canonical_profile_json, display_version, roster_digest,
                scanner_schema_version, calendar_dataset_version,
                calendar_dataset_digest, cadence)
               VALUES (?, '{}', 'Scanner data v1', ?, 'historical_scan_record.v1',
                       'exchange-calendars-v1', ?, 'per-exchange month_end')""",
            (
                _STAGE_PROFILE_HASH,
                _STAGE_ROSTER_DIGEST,
                TradingCalendar().session_table_digest(),
            ),
        )
        conn.execute(
            """INSERT OR IGNORE INTO active_snapshot_profile
               (singleton_id, profile_hash, activation_seq, activated_at)
               VALUES (1, ?, 1, ?)""",
            (_STAGE_PROFILE_HASH, _STAGE_NOW.isoformat()),
        )
        digest = _stage_qualification_digest()
        conn.execute(
            """INSERT OR IGNORE INTO historical_source_qualifications (
                   contract_digest, source_versions_json, fixture_digest,
                   probe_definition_digest, probe_digest, qualified_at, passed,
                   failure_code, failure_reason
               ) VALUES (?, ?, ?, ?, ?, ?, 1, NULL, NULL)""",
            (
                digest,
                current_source_versions_json(),
                _STAGE_FIXTURE_DIGEST,
                _STAGE_PROBE_DEFINITION_DIGEST,
                "3" * 64,
                _STAGE_NOW.isoformat(),
            ),
        )


def test_bootstrap_worker_walks_its_stages_then_completes(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path / "backtest.db")
    job = repo.create_bootstrap_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    engine = worker_module.build_stage_walk_engine(
        job.id, repo, StrategyJobType.BOOTSTRAP
    )

    result = engine.run(job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.COMPLETE
    assert result.current_stage is None
    assert (
        result.status_version
        == claim.job.status_version
        + len(STAGE_SEQUENCES[StrategyJobType.BOOTSTRAP])
        + 1
    )


def test_preparation_worker_honours_cancellation_at_a_stage_boundary(
    tmp_path: Path,
) -> None:
    repo = _stage_repo(tmp_path / "backtest.db")
    job = repo.create_preparation_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    repo.request_strategy_job_cancellation(
        job.id, expected_version=claim.job.status_version
    )
    engine = worker_module.build_stage_walk_engine(
        job.id, repo, StrategyJobType.PREPARATION
    )

    result = engine.run(job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.CANCELLED
    assert result.current_stage is None


def test_stage_worker_refuses_a_job_of_another_type(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path / "backtest.db")
    job = repo.create_bootstrap_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None
    engine = worker_module.StageWalkEngine(repo, StrategyJobType.PREPARATION)

    result = engine.run(job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.FAILED
    assert result.failure_code is JobFailureCode.INTEGRITY_ERROR


def test_stage_worker_writes_are_fenced_by_a_stale_lease_generation(
    tmp_path: Path,
) -> None:
    moment = [datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)]
    repo = BacktestRepository(
        db.make_connect(lambda: tmp_path / "backtest.db"),
        clock=lambda: date(2026, 8, 21),
        instant_clock=lambda: moment[0],
    )
    repo.ensure_schema()
    stale = repo.acquire_or_renew_worker_lease("worker-a", ttl_seconds=30)
    job = repo.create_bootstrap_job()
    claim = repo.claim_next_strategy_job(lease=stale.fence)
    assert claim is not None
    moment[0] = moment[0] + timedelta(seconds=120)
    repo.acquire_or_renew_worker_lease("worker-b", ttl_seconds=30)
    engine = worker_module.StageWalkEngine(
        repo, StrategyJobType.BOOTSTRAP, lease=stale.fence
    )

    result = engine.run(job.id, claim.claim_token)

    assert result.status is StrategyJobStatus.RUNNING
    assert result.current_stage is None
    assert result.lease_generation == stale.generation


def test_main_dispatches_a_bootstrap_job_to_the_stage_walk_engine(
    tmp_path: Path,
) -> None:
    repo = _stage_repo(tmp_path / "backtest.db")
    job = repo.create_bootstrap_job()
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    exit_code = main(
        ["--job-id", job.id, "--claim-token", claim.claim_token],
        repository_factory=lambda: repo,
    )

    assert exit_code == 0
    assert repo.strategy_job(job.id).status is StrategyJobStatus.COMPLETE


def test_stage_engine_construction_failure_is_not_mislabeled_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Repository(ClaimedJob(job_type=StrategyJobType.BOOTSTRAP))

    def broken(*_args, **_kwargs):
        raise RuntimeError("missing bootstrap subtype")

    monkeypatch.setattr(worker_module, "build_stage_walk_engine", broken)

    assert (
        main(
            ["--job-id", "job-1", "--claim-token", "claim-1"],
            repository_factory=lambda: repo,  # type: ignore[arg-type]
        )
        == 1
    )
    assert repo.failures[0]["failure_code"] is JobFailureCode.INTEGRITY_ERROR
