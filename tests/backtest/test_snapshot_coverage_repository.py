from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Literal

import pytest

from app.repositories import db
from app.repositories.backtest_repo import (
    BacktestIntegrityError,
    BacktestRepository,
    QualificationResult,
    RosterCaptureCommit,
)
from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.snapshot_profile import (
    LegitimateExclusionProofV1,
    MonthlySnapshotCommitV1,
    ProfileDetectorV1,
    SnapshotMemberV1,
    SnapshotProfileV1,
    build_before_first_provider_observation,
)
from app.services.backtest.source_manifest import detector_source_manifests
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.strategy_job import StrategyJobConflict
from app.services.backtest.historical_data_qualification import (
    FIXTURE_CONTRACT_VERSION,
    REQUEST_CONTRACT_VERSION,
    current_source_versions_json,
)
import json
from app.services.backtest.historical_initialization_engine import (
    HistoricalInitializationEngine,
    InitializationMonthError,
)
from app.services.backtest.strategy_job import JobFailureCode, StrategyJobStatus


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "historical_scan_record_v1.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _authoritative_detectors() -> tuple[ProfileDetectorV1, ...]:
    manifests = detector_source_manifests(PROJECT_ROOT)
    return tuple(
        ProfileDetectorV1(
            detector_id=detector.detector_id,
            detector_api_version=detector.detector_api_version,
            detector_version=manifests[detector.detector_id].digest,
        )
        for detector in DETECTOR_REGISTRY
    )


def _profile(display: str = "Scanner data v1") -> SnapshotProfileV1:
    return SnapshotProfileV1(
        schema_version="snapshot_profile.v1",
        display_version=display,
        record_schema_version="historical_scan_record.v1",
        detectors=_authoritative_detectors(),
        roster_policy_version="ReconstructionRosterPolicyV1",
        roster_digest=DIGEST_A,
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


def _record(month: str = "2026-07") -> HistoricalScanRecordV1:
    original = HistoricalScanRecordV1.from_canonical_json(
        FIXTURE.read_bytes().rstrip(b"\n")
    )
    payload = original.model_dump(mode="python")
    provenance = dict(payload["provenance"])
    provenance["calendar_dataset_digest"] = TradingCalendar().session_table_digest()
    provenance["detector_versions"] = {
        item.detector_id: item.detector_version for item in _authoritative_detectors()
    }
    payload["provenance"] = provenance
    if month != "2026-07":
        session = {"2026-06": "2026-06-30", "2026-05": "2026-05-29"}[month]
        payload["snapshot_month"] = month
        payload["as_of_session_date"] = session
    provisional = HistoricalScanRecordV1.model_validate(payload, strict=False)
    revision = _evidence_for_record(provisional).data_revision
    provenance = dict(provisional.provenance.model_dump(mode="python"))
    provenance["provider_data_revision"] = revision
    provenance["provider_evidence_manifest_digest"] = revision
    payload = provisional.model_dump(mode="python")
    payload["provenance"] = provenance
    return HistoricalScanRecordV1.model_validate(payload)


def _stored_evidence(
    *,
    security_id: str,
    symbol: str,
    session: date,
    start: str,
    end: str,
    request_contract_version: str,
    currency: str = "USD",
    quote_unit: str = "USD",
) -> StoredHistoricalEvidence:
    request = {
        "start": start,
        "end": end,
        "interval": "1d",
        "prepost": False,
        "auto_adjust": False,
        "back_adjust": False,
        "actions": True,
        "repair": False,
        "keepna": True,
        "rounding": False,
        "timeout": 15,
        "raise_errors": True,
    }
    rows = (
        {
            "session": session.isoformat(),
            "open": float(100).hex(),
            "high": float(102).hex(),
            "low": float(99).hex(),
            "close": float(101).hex(),
            "adj_close": float(101).hex(),
            "volume": float(1000).hex(),
            "dividends": float(0).hex(),
            "stock_splits": float(0).hex(),
        },
    )
    manifest = {
        "canonicalizer_version": "HistoricalEvidenceCanonicalizerV1",
        "request_contract_version": request_contract_version,
        "request": request,
        "requested_symbol": symbol,
        "observed_symbol": symbol,
        "currency": currency,
        "quote_unit": quote_unit,
        "quote_unit_scale": "1",
        "exchange_timezone": "America/New_York",
        "rows": rows,
        "provider": "yfinance",
        "provider_version": "1.4.1",
        "security_id": security_id,
        "alias_revision": DIGEST_B,
        "actions": (),
    }
    rendered = canonical_json(manifest)
    return StoredHistoricalEvidence(
        data_revision=manifest_digest(manifest),
        security_id=security_id,
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version=request_contract_version,
        requested_symbol=symbol,
        observed_symbol=symbol,
        alias_revision=DIGEST_B,
        currency=currency,
        quote_unit=quote_unit,
        quote_unit_scale="1",
        exchange_timezone="America/New_York",
        start=start,
        end=end,
        request_contract=request,
        response_metadata_digest=DIGEST_C,
        canonical_manifest_json=rendered,
        rows=rows,
        actions=(),
    )


def _evidence_for_record(record: HistoricalScanRecordV1) -> StoredHistoricalEvidence:
    return _stored_evidence(
        security_id=record.security_id,
        symbol=record.observed_symbol,
        session=record.as_of_session_date,
        start=f"{record.as_of_session_date.year}-01-01",
        end=date.fromordinal(record.as_of_session_date.toordinal() + 1).isoformat(),
        request_contract_version=record.provenance.provider_request_contract_version,
        currency=record.currency,
        quote_unit=record.quote_unit,
    )


def _snapshot(
    profile: SnapshotProfileV1,
    month: str = "2026-07",
    *,
    record: HistoricalScanRecordV1 | None = None,
    provenance_quality: Literal[
        "best_effort_reconstructed", "observed_bau"
    ] = "best_effort_reconstructed",
    source_run_id: str | None = None,
    observed_at: datetime | None = None,
) -> MonthlySnapshotCommitV1:
    value = record or _record(month)
    return MonthlySnapshotCommitV1.build(
        profile=profile,
        snapshot_month=month,
        provenance_quality=provenance_quality,
        members=(SnapshotMemberV1.valid_scan(value),),
        records=(value,),
        committed_at=NOW,
        as_of=date(2026, 8, 11),
        source_run_id=source_run_id,
        observed_at=observed_at,
    )


def _roster_commit() -> RosterCaptureCommit:
    return RosterCaptureCommit(
        lineage_id="lineage-1",
        roster_digest=DIGEST_A,
        roster_manifest_json='{"schema_version":"ReconstructionRosterManifestV1"}',
        policy_version="ReconstructionRosterPolicyV1",
        identity_registry_revision="d" * 64,
        identity_registry_json='{"identities":[]}',
        identity_evidence_digest="e" * 64,
        alias_revision=DIGEST_B,
        alias_manifest_json='{"entries":[]}',
        alias_evidence_digest="f" * 64,
        captured_at=NOW.isoformat(),
        identities=(("sec-001", "XNAS", "CAFÉ", "1" * 64),),
        aliases=(
            (
                "sec-001",
                "yfinance",
                "XNAS",
                "CAFÉ",
                None,
                None,
                "fixture",
                "4" * 64,
                "provider_evidence",
            ),
        ),
        sources=(("datahub_sp500", "2" * 64, "[]", NOW.isoformat()),),
        members=(
            (
                "sec-001",
                "XNAS",
                "CAFÉ",
                "USD",
                '["datahub_sp500"]',
                "[]",
                "3" * 64,
            ),
        ),
    )


def _repo(path, *, clock=lambda: date(2026, 8, 11)) -> BacktestRepository:
    repo = BacktestRepository(db.make_connect(lambda: path), clock=clock)
    repo.ensure_schema()
    if repo.roster_digest_for_lineage("lineage-1") is None:
        repo.commit_roster_capture(_roster_commit())
    return repo


class _Verifier:
    def __init__(
        self,
        snapshot: MonthlySnapshotCommitV1,
        evidence: tuple[StoredHistoricalEvidence, ...] = (),
    ) -> None:
        self._evidence = {item.data_revision: item for item in evidence}
        self._evidence.update(
            {
                item.data_revision: item
                for item in (
                    _evidence_for_record(record) for record in snapshot.records
                )
            }
        )

    def verify(self, data_revision: str) -> StoredHistoricalEvidence:
        return self._evidence[data_revision]


def _commit(
    repo: BacktestRepository,
    snapshot: MonthlySnapshotCommitV1,
    evidence: tuple[StoredHistoricalEvidence, ...] = (),
):
    return repo.commit_snapshot_month(snapshot, _Verifier(snapshot, evidence))


def _record_current_qualification(repo: BacktestRepository) -> str:
    fixture_digest = "1" * 64
    probe_definition_digest = "2" * 64
    source_versions_json = current_source_versions_json()
    qualification_digest = manifest_digest(
        {
            "sources": json.loads(source_versions_json),
            "calendar_digest": TradingCalendar().session_table_digest(),
            "request_contract": REQUEST_CONTRACT_VERSION,
            "fixture_contract": FIXTURE_CONTRACT_VERSION,
            "fixture_digest": fixture_digest,
            "probe_definition_digest": probe_definition_digest,
        }
    )
    repo.record_qualification(
        QualificationResult(
            contract_digest=qualification_digest,
            source_versions_json=source_versions_json,
            fixture_digest=fixture_digest,
            probe_definition_digest=probe_definition_digest,
            probe_digest="3" * 64,
            qualified_at=NOW.isoformat(),
            passed=True,
            failure_code=None,
            failure_reason=None,
        )
    )
    return qualification_digest


def test_profile_month_commit_reopens_and_sql_evidence_is_immutable(tmp_path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    profile = _profile()
    snapshot = _snapshot(profile)
    first = _commit(repo, snapshot)
    second = _commit(repo, snapshot)
    assert first == second == snapshot.manifest
    later_attempt = MonthlySnapshotCommitV1.build(
        profile=profile,
        snapshot_month="2026-07",
        provenance_quality="best_effort_reconstructed",
        members=snapshot.members,
        records=snapshot.records,
        committed_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        as_of=date(2026, 8, 12),
    )
    assert later_attempt.manifest.content_digest == first.content_digest
    assert _commit(repo, later_attempt) == first
    assert _repo(path).snapshot_month(profile.profile_hash, "2026-07") == first

    conn = repo._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_members").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM monthly_scan_results").fetchone() == (
            1,
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE snapshot_months SET valid_count=0")
    finally:
        conn.close()


def test_reconciled_stale_claim_cannot_publish_snapshot_month(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    snapshot = _snapshot(profile)
    with repo._connect() as conn:
        conn.execute(
            """INSERT INTO strategy_jobs (
                   id, job_type, status, enqueue_seq, claim_token, status_version,
                   created_at, updated_at
               ) VALUES ('job-1', 'backtest', 'running', 1, 'claim-1', 2, ?, ?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
    repo.reconcile_interrupted_strategy_jobs()

    with pytest.raises(StrategyJobConflict, match="no longer owns"):
        repo.commit_snapshot_month(
            snapshot,
            _Verifier(snapshot),
            job_claim=("job-1", "claim-1"),
        )

    with repo._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_profiles").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM snapshot_months").fetchone() == (0,)


def test_ready_snapshot_interval_returns_real_initialization_no_op(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    snapshot = _snapshot(profile)
    _commit(repo, snapshot)
    repo.activate_snapshot_profile(profile.profile_hash, NOW)
    qualification_digest = _record_current_qualification(repo)

    result = repo.create_initialization_job(
        profile_hash=profile.profile_hash,
        requested_start="2026-07",
        requested_end="2026-07",
        calendar_dataset_version=profile.calendar_dataset_version,
        qualification_contract_digest=qualification_digest,
    )

    assert result.no_op is True
    assert repo.list_strategy_jobs() == ()


def test_failed_later_month_retains_earlier_committed_snapshot(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    repo.compare_and_insert_snapshot_profile(profile)
    repo.activate_snapshot_profile(profile.profile_hash, NOW)
    qualification_digest = _record_current_qualification(repo)
    queued = repo.create_initialization_job(
        profile_hash=profile.profile_hash,
        requested_start="2026-05",
        requested_end="2026-06",
        calendar_dataset_version=profile.calendar_dataset_version,
        qualification_contract_digest=qualification_digest,
    ).job
    assert queued is not None
    claim = repo.claim_next_strategy_job()
    assert claim is not None

    def process(month: str) -> None:
        if month == "2026-06":
            raise InitializationMonthError(
                JobFailureCode.REQUIRED_DATA_MISSING,
                "Required historical data is unavailable",
            )
        _commit(repo, _snapshot(profile, month))

    result = HistoricalInitializationEngine(repo, process).run(
        claim.job.id, claim.claim_token
    )

    assert result.status is StrategyJobStatus.FAILED
    assert repo.snapshot_month(profile.profile_hash, "2026-05") is not None
    assert repo.snapshot_month(profile.profile_hash, "2026-06") is None


def test_commit_rejects_roster_mismatch_and_rolls_back_all_rows(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    bad = _snapshot(profile)
    bad_member = bad.members[0].model_copy(update={"security_id": "extra"})
    object.__setattr__(bad, "members", (bad_member,))
    with pytest.raises(BacktestIntegrityError):
        _commit(repo, bad)
    conn = repo._connect()
    try:
        for table in (
            "snapshot_profiles",
            "snapshot_months",
            "snapshot_members",
            "monthly_scan_results",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    finally:
        conn.close()


def test_commit_reverifies_provider_evidence_before_opening_transaction(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    snapshot = _snapshot(_profile())

    class WrongVerifier:
        def verify(self, data_revision: str) -> StoredHistoricalEvidence:
            evidence = _evidence_for_record(snapshot.records[0])
            return replace(
                evidence,
                security_id="wrong-security",
            )

    with pytest.raises(BacktestIntegrityError, match="provider evidence"):
        repo.commit_snapshot_month(snapshot, WrongVerifier())
    conn = repo._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_profiles").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM snapshot_months").fetchone() == (0,)
    finally:
        conn.close()


def test_commit_preserves_verifier_failure_code(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    snapshot = _snapshot(_profile())

    class MissingEvidence(RuntimeError):
        code = "required_data_missing"

    class MissingVerifier:
        def verify(self, data_revision: str) -> StoredHistoricalEvidence:
            raise MissingEvidence(data_revision)

    with pytest.raises(BacktestIntegrityError) as error:
        repo.commit_snapshot_month(snapshot, MissingVerifier())
    assert error.value.code == "required_data_missing"


def test_profile_commit_rejects_fabricated_calendar_and_detector_authority(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    with pytest.raises(BacktestIntegrityError) as calendar_error:
        repo.compare_and_insert_snapshot_profile(
            profile.model_copy(update={"calendar_dataset_digest": DIGEST_C})
        )
    assert calendar_error.value.code == "calendar_error"

    detectors = list(profile.detectors)
    detectors[0] = detectors[0].model_copy(update={"detector_api_version": "2"})
    with pytest.raises(BacktestIntegrityError, match="detector manifests"):
        repo.compare_and_insert_snapshot_profile(
            profile.model_copy(update={"detectors": tuple(detectors)})
        )


def test_repository_clock_rejects_caller_claim_that_current_month_is_closed(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "backtest.db", clock=lambda: date(2026, 7, 15))
    snapshot = _snapshot(_profile())
    with pytest.raises(BacktestIntegrityError) as error:
        _commit(repo, snapshot)
    assert error.value.code == "calendar_error"


def test_legitimate_exclusion_commits_member_without_scan_result(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile_values = _profile().model_dump(mode="python")
    profile_values["calendar_dataset_digest"] = TradingCalendar().session_table_digest()
    profile_values["yfinance_request_contract_version"] = (
        "YFinanceDailyProviderNativeV1"
    )
    profile = SnapshotProfileV1.model_validate(profile_values)
    evidence = _stored_evidence(
        security_id="sec-001",
        symbol="CAFÉ",
        session=date(2026, 8, 3),
        start="1970-01-01",
        end="2026-08-04",
        request_contract_version="YFinanceDailyProviderNativeV1",
    )
    proof = build_before_first_provider_observation(
        evidence=evidence,
        snapshot_month="2026-07",
        target_session=date(2026, 7, 31),
        mic="XNAS",
        alias_revision=DIGEST_B,
        alias_effective_from=None,
        alias_effective_to=None,
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=profile.calendar_dataset_digest,
        acquired_at=NOW,
    )
    snapshot = MonthlySnapshotCommitV1.build(
        profile=profile,
        snapshot_month="2026-07",
        provenance_quality="best_effort_reconstructed",
        members=(SnapshotMemberV1.legitimate_exclusion(proof),),
        records=(),
        committed_at=NOW,
        as_of=date(2026, 8, 11),
    )
    manifest = _commit(repo, snapshot, (evidence,))
    assert manifest.expected_count == manifest.excluded_count == 1
    assert manifest.valid_count == 0
    conn = repo._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_members").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM monthly_scan_results").fetchone() == (
            0,
        )
    finally:
        conn.close()


def test_commit_rebuilds_exclusion_proof_from_verified_rows(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile_values = _profile().model_dump(mode="python")
    profile_values["yfinance_request_contract_version"] = (
        "YFinanceDailyProviderNativeV1"
    )
    profile = SnapshotProfileV1.model_validate(profile_values)
    evidence = _stored_evidence(
        security_id="sec-001",
        symbol="CAFÉ",
        session=date(2026, 8, 3),
        start="1970-01-01",
        end="2026-08-04",
        request_contract_version="YFinanceDailyProviderNativeV1",
    )
    proof = build_before_first_provider_observation(
        evidence=evidence,
        snapshot_month="2026-07",
        target_session=date(2026, 7, 31),
        mic="XNAS",
        alias_revision=DIGEST_B,
        alias_effective_from=None,
        alias_effective_to=None,
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=profile.calendar_dataset_digest,
        acquired_at=NOW,
    )
    invented_values = proof.model_dump(mode="python")
    invented_values["first_observed_session"] = date(2026, 8, 1)
    invented = LegitimateExclusionProofV1.model_validate(invented_values)
    snapshot = MonthlySnapshotCommitV1.build(
        profile=profile,
        snapshot_month="2026-07",
        provenance_quality="best_effort_reconstructed",
        members=(SnapshotMemberV1.legitimate_exclusion(invented),),
        records=(),
        committed_at=NOW,
        as_of=date(2026, 8, 11),
    )
    with pytest.raises(BacktestIntegrityError, match="not derived"):
        _commit(repo, snapshot, (evidence,))


def test_commit_rejects_symbol_without_effective_immutable_alias(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    payload = _record().model_dump(mode="python")
    payload["observed_symbol"] = "OTHER"
    provisional = HistoricalScanRecordV1.model_validate(payload)
    evidence = _evidence_for_record(provisional)
    provenance = dict(provisional.provenance.model_dump(mode="python"))
    provenance["provider_data_revision"] = evidence.data_revision
    provenance["provider_evidence_manifest_digest"] = evidence.data_revision
    payload["provenance"] = provenance
    record = HistoricalScanRecordV1.model_validate(payload)
    snapshot = _snapshot(_profile(), record=record)
    with pytest.raises(BacktestIntegrityError) as error:
        _commit(repo, snapshot)
    assert error.value.code == "identity_ambiguous"


def test_identical_concurrent_commit_converges_and_conflict_never_overwrites(
    tmp_path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    profile = _profile()
    snapshot = _snapshot(profile)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: _commit(repo, snapshot), range(12)))
    assert all(result == snapshot.manifest for result in results)

    changed_record = _record().model_copy(
        update={
            "technicals": _record().technicals.model_copy(
                update={"price": Decimal("124")}
            )
        }
    )
    changed = _snapshot(profile, record=changed_record)
    with pytest.raises(BacktestIntegrityError, match="conflicting"):
        _commit(repo, changed)
    assert repo.snapshot_month(profile.profile_hash, "2026-07") == snapshot.manifest


def test_active_profile_pointer_is_monotonic_and_idempotent(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    first = _profile()
    second = _profile("Scanner data v2")
    repo.compare_and_insert_snapshot_profile(first)
    repo.compare_and_insert_snapshot_profile(second)
    active1 = repo.activate_snapshot_profile(first.profile_hash, NOW)
    active1_again = repo.activate_snapshot_profile(first.profile_hash, NOW)
    active2 = repo.activate_snapshot_profile(second.profile_hash, NOW)
    assert active1.activation_seq == active1_again.activation_seq == 1
    assert active2.activation_seq == 2
    assert repo.active_snapshot_profile() == active2
    with pytest.raises(BacktestIntegrityError):
        repo.activate_snapshot_profile("f" * 64, NOW)
    conn = repo._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM active_snapshot_profile")
    finally:
        conn.close()


def test_coverage_never_bridges_gaps_or_profiles_and_readiness_is_exact(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    other = _profile("Scanner data v2")
    for month in ("2026-05", "2026-07"):
        value = _snapshot(profile, month)
        _commit(repo, value)
    other_value = _snapshot(other, "2026-06")
    _commit(repo, other_value)
    repo.activate_snapshot_profile(profile.profile_hash, NOW)

    coverage = repo.snapshot_coverage()
    assert coverage.profile_hash == profile.profile_hash
    assert coverage.earliest_month == "2026-05"
    assert coverage.latest_month == "2026-07"
    assert coverage.snapshot_count == 2
    assert tuple((item.start_month, item.end_month) for item in coverage.intervals) == (
        ("2026-05", "2026-05"),
        ("2026-07", "2026-07"),
    )
    partial = repo.interval_readiness(profile.profile_hash, "2026-05", "2026-07")
    assert partial.ready is False
    assert partial.no_op is False
    assert partial.missing_months == ("2026-06",)
    assert partial.ordered_month_digest is None
    ready = repo.interval_readiness(profile.profile_hash, "2026-07", "2026-07")
    assert ready.ready is ready.no_op is True
    assert ready.missing_months == ()
    assert ready.ordered_month_digest is not None


def test_coverage_cache_reuses_verified_summary_and_separates_profiles(
    tmp_path, monkeypatch
) -> None:
    """The verification count is the deterministic performance evidence."""
    repo = _repo(tmp_path / "backtest.db")
    first = _profile()
    second = _profile("Scanner data v2")
    _commit(repo, _snapshot(first))
    _commit(repo, _snapshot(second))

    calls = 0
    original = repo._load_verified_snapshot_month

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "_load_verified_snapshot_month", counted)
    assert repo.snapshot_coverage(first.profile_hash).profile_hash == first.profile_hash
    assert repo.snapshot_coverage(first.profile_hash).profile_hash == first.profile_hash
    assert (
        repo.snapshot_coverage(second.profile_hash).profile_hash == second.profile_hash
    )
    assert calls == 2


def test_coverage_cache_invalidates_after_commit_and_reconstructs_after_restart(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    profile = _profile()
    _commit(repo, _snapshot(profile, "2026-05"))
    calls = 0
    original = repo._load_verified_snapshot_month

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "_load_verified_snapshot_month", counted)
    repo.snapshot_coverage(profile.profile_hash)
    repo.snapshot_coverage(profile.profile_hash)
    assert calls == 1

    _commit(repo, _snapshot(profile, "2026-07"))
    coverage = repo.snapshot_coverage(profile.profile_hash)
    assert coverage.snapshot_count == 2
    assert calls == 3

    reopened = _repo(path)
    reopened_calls = 0
    reopened_original = reopened._load_verified_snapshot_month

    def reopened_counted(*args, **kwargs):
        nonlocal reopened_calls
        reopened_calls += 1
        return reopened_original(*args, **kwargs)

    monkeypatch.setattr(reopened, "_load_verified_snapshot_month", reopened_counted)
    assert reopened.snapshot_coverage(profile.profile_hash).snapshot_count == 2
    assert reopened_calls == 2


def test_coverage_cache_never_masks_corruption_and_serializes_readers(
    tmp_path, monkeypatch
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    _commit(repo, _snapshot(profile))
    calls = 0
    original = repo._load_verified_snapshot_month

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "_load_verified_snapshot_month", counted)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda _: repo.snapshot_coverage(profile.profile_hash), range(6))
        )
    assert all(result == results[0] for result in results)
    assert calls == 1

    conn = repo._connect()
    try:
        conn.execute("DROP TRIGGER snapshot_month_immutable_update")
        conn.execute(
            "UPDATE snapshot_months SET expected_digest=?",
            (DIGEST_B,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(BacktestIntegrityError):
        repo.snapshot_coverage(profile.profile_hash)
    assert calls == 2


def test_coverage_reverifies_denormalized_month_columns_and_digests(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    _commit(repo, _snapshot(profile))
    conn = repo._connect()
    try:
        conn.execute("DROP TRIGGER snapshot_month_immutable_update")
        conn.execute(
            "UPDATE snapshot_months SET expected_digest=?",
            (DIGEST_B,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(BacktestIntegrityError, match="key"):
        repo.snapshot_coverage(profile.profile_hash)


def test_coverage_rejects_missing_result_even_after_direct_database_tamper(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    _commit(repo, _snapshot(profile))
    conn = repo._connect()
    try:
        conn.execute("DROP TRIGGER monthly_scan_result_immutable_delete")
        conn.execute("DELETE FROM monthly_scan_results")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(BacktestIntegrityError, match="write set"):
        repo.snapshot_coverage(profile.profile_hash)


def test_explicit_profile_coverage_can_be_empty_without_fabricated_intervals(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    repo.compare_and_insert_snapshot_profile(profile)
    coverage = repo.snapshot_coverage(profile.profile_hash)
    assert coverage.earliest_month is None
    assert coverage.latest_month is None
    assert coverage.snapshot_count == 0
    assert coverage.intervals == ()
    assert coverage.provenance == ()


def test_reconstructed_and_observed_months_share_profile_without_blending_provenance(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "backtest.db")
    profile = _profile()
    reconstructed = _snapshot(profile, "2026-05")
    _commit(repo, reconstructed)
    observed_payload = _record("2026-06").model_dump(mode="python")
    observed_payload["provenance_quality"] = "observed_bau"
    observed_record = HistoricalScanRecordV1.model_validate(observed_payload)
    observed = _snapshot(
        profile,
        "2026-06",
        record=observed_record,
        provenance_quality="observed_bau",
        source_run_id="bau-run-1",
        observed_at=NOW,
    )
    _commit(repo, observed)
    coverage = repo.snapshot_coverage(profile.profile_hash)
    assert coverage.snapshot_count == 2
    assert tuple(item.provenance_quality for item in coverage.provenance) == (
        "best_effort_reconstructed",
        "observed_bau",
    )
    assert tuple(item.snapshot_count for item in coverage.provenance) == (1, 1)


def test_snapshot_profile_import_graph_stays_outside_live_portfolio() -> None:
    source = Path("app/services/backtest/snapshot_profile.py").read_text()
    forbidden = (
        "from app.agents",
        "import app.agents",
        "TraderAgent",
        "trades_repo",
        "portfolio_repo",
        "cash_repo",
        "order_submission",
        "notifications_repo",
        "strategy_job_service",
    )
    assert all(token not in source for token in forbidden)
