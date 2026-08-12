from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.snapshot_profile import (
    LegitimateExclusionProofV1,
    MonthlySnapshotCommitV1,
    ProfileDetectorV1,
    SnapshotContractError,
    SnapshotMemberV1,
    SnapshotProfileV1,
    build_before_first_provider_observation,
)
from app.services.backtest.trading_calendar import TradingCalendar


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
FIXTURE = Path(__file__).parent / "fixtures" / "historical_scan_record_v1.json"


def _profile(**changes: object) -> SnapshotProfileV1:
    values: dict[str, object] = {
        "schema_version": "snapshot_profile.v1",
        "display_version": "Scanner data v1",
        "record_schema_version": "historical_scan_record.v1",
        "detectors": (
            ProfileDetectorV1(
                detector_id="technical_indicators_v1",
                detector_api_version="1",
                detector_version=DIGEST_A,
            ),
            ProfileDetectorV1(
                detector_id="weinstein_stage_v1",
                detector_api_version="1",
                detector_version=DIGEST_B,
            ),
            ProfileDetectorV1(
                detector_id="vcp_v1",
                detector_api_version="1",
                detector_version=DIGEST_C,
            ),
        ),
        "roster_policy_version": "ReconstructionRosterPolicyV1",
        "roster_digest": DIGEST_A,
        "identity_registry_version": "SecurityIdentityRegistryV1",
        "alias_policy_version": "SecurityAliasManifestV1",
        "source_policy_version": "FreeHistoricalSourcePolicyV1",
        "calendar_policy_version": "PerExchangeMonthEndV1",
        "calendar_dataset_version": "exchange-calendars-v1",
        "calendar_dataset_digest": DIGEST_C,
        "yfinance_request_contract_version": "yfinance-daily-v1",
        "yfinance_ingestion_version": "ingestion-v1",
        "market_plane_policy_version": "HistoricalMarketPlanesV1",
        "reconstructability_policy_version": "reconstructability.v1",
        "provenance_vocabulary": (
            "best_effort_reconstructed",
            "observed_bau",
        ),
        "cadence": "per-exchange month_end",
    }
    values.update(changes)
    return SnapshotProfileV1.model_validate(values)


def _record() -> HistoricalScanRecordV1:
    return HistoricalScanRecordV1.from_canonical_json(
        FIXTURE.read_bytes().rstrip(b"\n")
    )


def _full_history_evidence(
    first_session: str = "2026-08-03",
) -> StoredHistoricalEvidence:
    rows = (
        {
            "session": first_session,
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
    request = {
        "start": "1970-01-01",
        "end": "2026-08-04",
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
    manifest = {
        "canonicalizer_version": "HistoricalEvidenceCanonicalizerV1",
        "request_contract_version": "YFinanceDailyProviderNativeV1",
        "request": request,
        "requested_symbol": "TEST",
        "observed_symbol": "TEST",
        "currency": "USD",
        "quote_unit": "USD",
        "quote_unit_scale": "1",
        "exchange_timezone": "America/New_York",
        "rows": rows,
        "provider": "yfinance",
        "provider_version": "1.4.1",
        "security_id": "sec-001",
        "alias_revision": DIGEST_B,
        "actions": (),
    }
    rendered = canonical_json(manifest)
    return StoredHistoricalEvidence(
        data_revision=manifest_digest(manifest),
        security_id="sec-001",
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version="YFinanceDailyProviderNativeV1",
        requested_symbol="TEST",
        observed_symbol="TEST",
        alias_revision=DIGEST_B,
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1",
        exchange_timezone="America/New_York",
        start="1970-01-01",
        end="2026-08-04",
        request_contract=request,
        response_metadata_digest=DIGEST_C,
        canonical_manifest_json=rendered,
        rows=rows,
        actions=(),
    )


def _proof(
    *,
    evidence: StoredHistoricalEvidence | None = None,
    alias_revision: str = DIGEST_B,
    calendar_dataset_digest: str | None = None,
    acquired_at: datetime = datetime(2026, 8, 11, tzinfo=timezone.utc),
) -> LegitimateExclusionProofV1:
    return build_before_first_provider_observation(
        evidence=evidence or _full_history_evidence(),
        snapshot_month="2026-07",
        target_session=date(2026, 7, 31),
        mic="XNAS",
        alias_revision=alias_revision,
        alias_effective_from=None,
        alias_effective_to=None,
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=(
            TradingCalendar().session_table_digest()
            if calendar_dataset_digest is None
            else calendar_dataset_digest
        ),
        acquired_at=acquired_at,
    )


def test_profile_identity_is_canonical_ordered_and_deeply_immutable() -> None:
    profile = _profile(detectors=tuple(reversed(_profile().detectors)))
    assert tuple(item.detector_id for item in profile.detectors) == (
        "technical_indicators_v1",
        "weinstein_stage_v1",
        "vcp_v1",
    )
    assert len(profile.profile_hash) == 64
    assert SnapshotProfileV1.from_canonical_json(profile.canonical_json()) == profile
    with pytest.raises(ValidationError):
        _profile(provenance_vocabulary=("observed_bau",))


def test_complete_valid_month_builds_balanced_canonical_digests() -> None:
    profile = _profile()
    record = _record()
    member = SnapshotMemberV1.valid_scan(record)
    commit = MonthlySnapshotCommitV1.build(
        profile=profile,
        snapshot_month="2026-07",
        provenance_quality="best_effort_reconstructed",
        members=(member,),
        records=(record,),
        committed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        as_of=date(2026, 8, 11),
    )
    assert commit.manifest.expected_count == 1
    assert commit.manifest.valid_count == 1
    assert commit.manifest.excluded_count == 0
    assert len(commit.manifest.content_digest) == 64
    assert commit.profile_hash == profile.profile_hash
    assert commit.canonical_json() == commit.canonical_json()


def test_month_rejects_missing_extra_or_mismatched_scan_records() -> None:
    profile = _profile()
    record = _record()
    member = SnapshotMemberV1.valid_scan(record)
    with pytest.raises(SnapshotContractError, match="valid member requires one record"):
        MonthlySnapshotCommitV1.build(
            profile=profile,
            snapshot_month="2026-07",
            provenance_quality="best_effort_reconstructed",
            members=(member,),
            records=(),
            committed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            as_of=date(2026, 8, 11),
        )


def test_month_build_rejects_current_or_noncanonical_member_session() -> None:
    profile = _profile()
    record = _record()
    member = SnapshotMemberV1.valid_scan(record)
    with pytest.raises(SnapshotContractError, match="fully closed") as error:
        MonthlySnapshotCommitV1.build(
            profile=profile,
            snapshot_month="2026-07",
            provenance_quality="best_effort_reconstructed",
            members=(member,),
            records=(record,),
            committed_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            as_of=date(2026, 7, 15),
        )
    assert error.value.code == "calendar_error"
    wrong = member.model_copy(update={"as_of_session_date": date(2026, 7, 30)})
    with pytest.raises(SnapshotContractError, match="canonical month end"):
        MonthlySnapshotCommitV1.build(
            profile=profile,
            snapshot_month="2026-07",
            provenance_quality="best_effort_reconstructed",
            members=(wrong,),
            records=(record,),
            committed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            as_of=date(2026, 8, 11),
        )
    with pytest.raises(SnapshotContractError, match="duplicate member"):
        MonthlySnapshotCommitV1.build(
            profile=profile,
            snapshot_month="2026-07",
            provenance_quality="best_effort_reconstructed",
            members=(member, member),
            records=(record,),
            committed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            as_of=date(2026, 8, 11),
        )


def test_before_first_observation_requires_exact_full_history_proof() -> None:
    evidence = _full_history_evidence()
    proof = build_before_first_provider_observation(
        evidence=evidence,
        snapshot_month="2026-07",
        target_session=date(2026, 7, 31),
        mic="XNAS",
        alias_revision=DIGEST_B,
        alias_effective_from=None,
        alias_effective_to=None,
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=TradingCalendar().session_table_digest(),
        acquired_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    assert isinstance(proof, LegitimateExclusionProofV1)
    assert proof.exclusion_reason == "before_first_provider_observation"
    assert proof.provider_observed_lifetime_only is True
    assert proof.verified_listing_date is False

    equal_evidence = _full_history_evidence("2026-07-31")
    with pytest.raises(SnapshotContractError, match="precede"):
        build_before_first_provider_observation(
            evidence=equal_evidence,
            snapshot_month="2026-07",
            target_session=date(2026, 7, 31),
            mic="XNAS",
            alias_revision=DIGEST_B,
            alias_effective_from=None,
            alias_effective_to=None,
            calendar_dataset_version="exchange-calendars-v1",
            calendar_dataset_digest=TradingCalendar().session_table_digest(),
            acquired_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )

    bounded = replace(evidence, start="2026-01-01")
    with pytest.raises(SnapshotContractError, match="full-history"):
        build_before_first_provider_observation(
            evidence=bounded,
            snapshot_month="2026-07",
            target_session=date(2026, 7, 31),
            mic="XNAS",
            alias_revision=DIGEST_B,
            alias_effective_from=None,
            alias_effective_to=None,
            calendar_dataset_version="exchange-calendars-v1",
            calendar_dataset_digest=TradingCalendar().session_table_digest(),
            acquired_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )


def test_exclusion_rejects_tampered_identity_alias_calendar_and_manifest() -> None:
    evidence = _full_history_evidence()
    with pytest.raises(SnapshotContractError):
        _proof(evidence=evidence, alias_revision=DIGEST_A)
    with pytest.raises(SnapshotContractError):
        _proof(evidence=evidence, calendar_dataset_digest=DIGEST_A)
    with pytest.raises(SnapshotContractError):
        _proof(evidence=replace(evidence, security_id="wrong"))
    with pytest.raises(SnapshotContractError):
        _proof(evidence=replace(evidence, canonical_manifest_json=json.dumps({})))


def test_exclusion_acquisition_time_is_audit_metadata_not_content_identity() -> None:
    evidence = _full_history_evidence()
    first = _proof(
        evidence=evidence,
        acquired_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    later = _proof(
        evidence=evidence,
        acquired_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    assert first.canonical_json() != later.canonical_json()
    assert first.content_digest == later.content_digest
    assert (
        SnapshotMemberV1.legitimate_exclusion(first).content_identity()
        == SnapshotMemberV1.legitimate_exclusion(later).content_identity()
    )


def test_evidence_manifest_rejects_relabelled_provider_fields_and_non_object_json() -> (
    None
):
    evidence = _full_history_evidence()
    with pytest.raises(SnapshotContractError, match="integrity"):
        _proof(evidence=replace(evidence, provider_version="invented"))
    with pytest.raises(SnapshotContractError, match="invalid"):
        _proof(evidence=replace(evidence, canonical_manifest_json="[]"))


def test_exclusion_rejects_non_session_and_noncanonical_observations() -> None:
    with pytest.raises(SnapshotContractError, match="observations"):
        _proof(evidence=_full_history_evidence("2026-08-02"))

    evidence = _full_history_evidence()
    malformed_rows = (dict(evidence.rows[0], high="nan"),)
    manifest = json.loads(evidence.canonical_manifest_json)
    manifest["rows"] = list(malformed_rows)
    malformed = replace(
        evidence,
        rows=malformed_rows,
        canonical_manifest_json=canonical_json(manifest),
        data_revision=manifest_digest(manifest),
    )
    with pytest.raises(SnapshotContractError, match="observations"):
        _proof(evidence=malformed)


def test_month_build_recomputes_member_source_and_provenance_fields() -> None:
    profile = _profile()
    record = _record()
    values = SnapshotMemberV1.valid_scan(record).model_dump(mode="python")
    values["source_payload_digest"] = DIGEST_B
    identity = dict(values)
    identity.pop("provenance_digest")
    identity["exclusion_evidence"] = None
    values["provenance_digest"] = manifest_digest(
        {"schema_version": "snapshot_member_provenance.v1", **identity}
    )
    tampered = SnapshotMemberV1.model_validate(values)
    with pytest.raises(SnapshotContractError, match="not canonical"):
        MonthlySnapshotCommitV1.build(
            profile=profile,
            snapshot_month="2026-07",
            provenance_quality="best_effort_reconstructed",
            members=(tampered,),
            records=(record,),
            committed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            as_of=date(2026, 8, 11),
        )


def test_observed_bau_source_run_identifier_cannot_be_empty() -> None:
    from app.services.backtest.snapshot_profile import SnapshotMonthManifestV1

    with pytest.raises(ValidationError):
        SnapshotMonthManifestV1(
            schema_version="snapshot_month_manifest.v1",
            profile_hash=DIGEST_A,
            snapshot_month="2026-07",
            provenance_quality="observed_bau",
            processing_complete=True,
            market_complete="unknown",
            roster_digest=DIGEST_A,
            expected_digest=DIGEST_A,
            input_revision_digest=DIGEST_A,
            result_digest=DIGEST_A,
            expected_count=0,
            valid_count=0,
            excluded_count=0,
            content_digest=DIGEST_A,
            source_run_id="",
            observed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            committed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
