"""Canonical policy profiles and complete monthly snapshot write sets."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import json
import math
from typing import Annotated, Literal, Protocol, cast

from pydantic import Field, field_validator, model_validator

from app.services.backtest.canonical_manifest import (
    canonical_json_digest,
    manifest_digest,
)
from app.services.backtest.historical_scan_record import (
    CanonicalModel,
    HistoricalScanRecordV1,
)
from app.services.backtest.trading_calendar import TradingCalendar

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]
SnapshotMonth = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
ProvenanceQuality = Literal["best_effort_reconstructed", "observed_bau"]
MemberResolution = Literal["valid_scan", "legitimate_exclusion"]
DetectorId = Literal["technical_indicators_v1", "weinstein_stage_v1", "vcp_v1"]
DETECTOR_ORDER = (
    "technical_indicators_v1",
    "weinstein_stage_v1",
    "vcp_v1",
)
PROVENANCE_VOCABULARY = (
    "best_effort_reconstructed",
    "observed_bau",
)
FULL_HISTORY_START = date(1970, 1, 1)


class SnapshotContractError(ValueError):
    """Stable typed failure at the monthly coverage boundary."""

    def __init__(self, message: str, *, code: str = "integrity_error") -> None:
        self.code = code
        super().__init__(message)


class HistoricalEvidenceV1(Protocol):
    @property
    def data_revision(self) -> str: ...

    @property
    def security_id(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def provider_version(self) -> str: ...

    @property
    def request_contract_version(self) -> str: ...

    @property
    def requested_symbol(self) -> str: ...

    @property
    def observed_symbol(self) -> str: ...

    @property
    def alias_revision(self) -> str | None: ...

    @property
    def currency(self) -> str: ...

    @property
    def quote_unit(self) -> str: ...

    @property
    def quote_unit_scale(self) -> str: ...

    @property
    def exchange_timezone(self) -> str: ...

    @property
    def start(self) -> str: ...

    @property
    def end(self) -> str: ...

    @property
    def request_contract(self) -> Mapping[str, object]: ...

    @property
    def canonical_manifest_json(self) -> str: ...

    @property
    def rows(self) -> tuple[Mapping[str, object], ...]: ...

    @property
    def actions(self) -> tuple[Mapping[str, object], ...]: ...


class ProfileDetectorV1(CanonicalModel):
    detector_id: DetectorId
    detector_api_version: NonEmpty
    detector_version: Digest


class SnapshotProfileV1(CanonicalModel):
    schema_version: Literal["snapshot_profile.v1"]
    display_version: NonEmpty
    record_schema_version: Literal["historical_scan_record.v1"]
    detectors: tuple[ProfileDetectorV1, ...]
    roster_policy_version: Literal["ReconstructionRosterPolicyV1"]
    roster_digest: Digest
    identity_registry_version: Literal["SecurityIdentityRegistryV1"]
    alias_policy_version: Literal["SecurityAliasManifestV1"]
    source_policy_version: NonEmpty
    calendar_policy_version: NonEmpty
    calendar_dataset_version: Literal["exchange-calendars-v1"]
    calendar_dataset_digest: Digest
    yfinance_request_contract_version: NonEmpty
    yfinance_ingestion_version: NonEmpty
    market_plane_policy_version: Literal["HistoricalMarketPlanesV1"]
    reconstructability_policy_version: Literal["reconstructability.v1"]
    provenance_vocabulary: tuple[ProvenanceQuality, ...]
    cadence: Literal["per-exchange month_end"]

    @field_validator("detectors")
    @classmethod
    def _closed_ordered_detectors(
        cls, value: tuple[ProfileDetectorV1, ...]
    ) -> tuple[ProfileDetectorV1, ...]:
        by_id = {item.detector_id: item for item in value}
        if len(value) != len(DETECTOR_ORDER) or set(by_id) != set(DETECTOR_ORDER):
            raise ValueError("profile requires the exact V1 detector set")
        return tuple(by_id[cast(DetectorId, name)] for name in DETECTOR_ORDER)

    @field_validator("provenance_vocabulary")
    @classmethod
    def _closed_provenance_vocabulary(
        cls, value: tuple[ProvenanceQuality, ...]
    ) -> tuple[ProvenanceQuality, ...]:
        if value != PROVENANCE_VOCABULARY:
            raise ValueError("profile requires the exact provenance vocabulary")
        return value

    @property
    def profile_hash(self) -> str:
        return self.digest()

    @property
    def detector_versions(self) -> dict[str, str]:
        return {item.detector_id: item.detector_version for item in self.detectors}


def adoption_gate_failures(
    previous: "SnapshotProfileV1", current: "SnapshotProfileV1"
) -> tuple[str, ...]:
    """Return the rebuild-forcing policy differences between two profiles.

    gh-468: roster churn alone (``roster_digest``/``alias_revision``) is the
    adoption case; any difference in the detector set/versions, ingestion
    version, calendar dataset, request contract, record schema, or roster
    policy invalidates carried evidence and forces a rebuild. Returned
    strings are user-facing reasons, empty when Update is allowed.
    """
    failures: list[str] = []
    previous_detectors = {
        item.detector_id: (item.detector_api_version, item.detector_version)
        for item in previous.detectors
    }
    for item in current.detectors:
        prior = previous_detectors.get(item.detector_id)
        if prior != (item.detector_api_version, item.detector_version):
            failures.append(
                f"detector {item.detector_id} changed between data versions"
            )
    if previous.yfinance_ingestion_version != current.yfinance_ingestion_version:
        failures.append("the ingestion version changed")
    if previous.calendar_dataset_version != current.calendar_dataset_version:
        failures.append("the trading calendar dataset changed")
    if previous.calendar_dataset_digest != current.calendar_dataset_digest:
        failures.append("the trading calendar data changed")
    if (
        previous.yfinance_request_contract_version
        != current.yfinance_request_contract_version
    ):
        failures.append("the price request contract changed")
    if previous.record_schema_version != current.record_schema_version:
        failures.append("the scan record schema changed")
    if previous.roster_policy_version != current.roster_policy_version:
        failures.append("the roster policy changed")
    return tuple(failures)


class LegitimateExclusionProofV1(CanonicalModel):
    schema_version: Literal[
        "before_first_provider_observation.v1",
        "insufficient_detector_history.v1",
        "incomplete_detector_history.v1",
    ]
    exclusion_reason: Literal[
        "before_first_provider_observation",
        "insufficient_detector_history",
        "incomplete_detector_history",
    ]
    security_id: NonEmpty
    requested_symbol: NonEmpty
    observed_symbol: NonEmpty
    alias_revision: Digest
    alias_effective_from: date | None
    alias_effective_to: date | None
    snapshot_month: SnapshotMonth
    mic: Literal["BATS", "XNAS", "XNYS", "XLON"]
    target_session: date
    calendar_dataset_version: Literal["exchange-calendars-v1"]
    calendar_dataset_digest: Digest
    provider: Literal["yfinance"]
    provider_version: NonEmpty
    request_contract_version: Literal["YFinanceDailyProviderNativeV1"]
    full_history_start: date
    full_history_end_exclusive: date
    evidence_revision: Digest
    evidence_manifest_digest: Digest
    first_observed_session: date
    currency: Literal["USD", "GBP"]
    quote_unit: Literal["USD", "GBP", "GBp"]
    acquired_at: datetime
    provider_observed_lifetime_only: Literal[True]
    verified_listing_date: Literal[False]
    required_history_sessions: Literal[252] | None = None
    available_history_sessions: int | None = None
    missing_session_digest: Digest | None = None

    @field_validator("acquired_at")
    @classmethod
    def _utc_acquisition(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("acquired_at must be a UTC instant")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _closed_boundary(self) -> "LegitimateExclusionProofV1":
        if self.snapshot_month != self.target_session.strftime("%Y-%m"):
            raise ValueError("target session is outside snapshot month")
        if self.full_history_start != FULL_HISTORY_START:
            raise ValueError("full-history request has the wrong start")
        if self.exclusion_reason == "before_first_provider_observation":
            if (
                self.schema_version != "before_first_provider_observation.v1"
                or self.target_session >= self.first_observed_session
                or self.required_history_sessions is not None
                or self.available_history_sessions is not None
                or self.missing_session_digest is not None
            ):
                raise ValueError("before-first exclusion boundary is invalid")
        elif self.exclusion_reason == "insufficient_detector_history":
            if (
                self.schema_version != "insufficient_detector_history.v1"
                or self.target_session < self.first_observed_session
                or self.required_history_sessions != 252
                or self.available_history_sessions is None
                or not 0 < self.available_history_sessions < 252
                or self.missing_session_digest is not None
            ):
                raise ValueError("insufficient-history exclusion boundary is invalid")
        elif self.exclusion_reason == "incomplete_detector_history":
            if (
                self.schema_version != "incomplete_detector_history.v1"
                or self.target_session < self.first_observed_session
                or self.required_history_sessions != 252
                or self.available_history_sessions is None
                or not 0 <= self.available_history_sessions < 252
                or self.missing_session_digest is None
            ):
                raise ValueError("incomplete-history exclusion boundary is invalid")
        else:
            raise ValueError("unsupported exclusion reason")
        if self.full_history_end_exclusive <= self.first_observed_session:
            raise ValueError("full-history request does not include first observation")
        if self.evidence_revision != self.evidence_manifest_digest:
            raise ValueError("evidence revision and manifest digest differ")
        if (
            self.alias_effective_from
            and self.target_session < self.alias_effective_from
        ):
            raise ValueError("alias is not effective on target session")
        if self.alias_effective_to and self.target_session >= self.alias_effective_to:
            raise ValueError("alias is not effective on target session")
        return self

    def content_identity(self) -> dict[str, object]:
        """Return exclusion evidence identity without acquisition audit time."""
        return self.model_dump(mode="python", exclude={"acquired_at"})

    @property
    def content_digest(self) -> str:
        return manifest_digest(self.content_identity())


class SnapshotMemberV1(CanonicalModel):
    schema_version: Literal["snapshot_member.v1"] = "snapshot_member.v1"
    security_id: NonEmpty
    observed_symbol: NonEmpty
    mic: Literal["BATS", "XNAS", "XNYS", "XLON"]
    as_of_session_date: date
    resolution: MemberResolution
    source_cutoff: date
    source_payload_digest: Digest
    input_revision: Digest
    provider_data_revision: Digest
    provider_evidence_manifest_digest: Digest
    alias_revision: Digest
    record_digest: Digest | None
    exclusion_reason: (
        Literal[
            "before_first_provider_observation",
            "insufficient_detector_history",
            "incomplete_detector_history",
        ]
        | None
    )
    exclusion_evidence: LegitimateExclusionProofV1 | None
    provenance_digest: Digest

    @model_validator(mode="after")
    def _resolution_shape(self) -> "SnapshotMemberV1":
        if self.source_cutoff > self.as_of_session_date:
            raise ValueError("source cutoff exceeds target session")
        if self.resolution == "valid_scan":
            if (
                self.record_digest is None
                or self.exclusion_reason is not None
                or self.exclusion_evidence is not None
            ):
                raise ValueError("valid member evidence is malformed")
            if self.source_cutoff != self.as_of_session_date:
                raise ValueError("valid member source cutoff is not the target session")
        elif (
            self.record_digest is not None
            or self.exclusion_reason
            not in {
                "before_first_provider_observation",
                "insufficient_detector_history",
                "incomplete_detector_history",
            }
            or self.exclusion_evidence is None
        ):
            raise ValueError("excluded member evidence is malformed")
        elif (
            self.exclusion_evidence.security_id != self.security_id
            or self.exclusion_evidence.observed_symbol != self.observed_symbol
            or self.exclusion_evidence.mic != self.mic
            or self.exclusion_evidence.target_session != self.as_of_session_date
            or self.exclusion_evidence.evidence_revision != self.provider_data_revision
            or self.exclusion_evidence.evidence_manifest_digest
            != self.provider_evidence_manifest_digest
            or self.exclusion_evidence.alias_revision != self.alias_revision
            or self.exclusion_evidence.content_digest != self.input_revision
            or self.source_cutoff != self.as_of_session_date
            or self.source_payload_digest != self.exclusion_evidence.evidence_revision
        ):
            raise ValueError("excluded member proof does not match member")
        identity_values = self.model_dump(
            mode="python", exclude={"provenance_digest", "exclusion_evidence"}
        )
        identity_values["exclusion_evidence"] = (
            None
            if self.exclusion_evidence is None
            else self.exclusion_evidence.content_identity()
        )
        expected_provenance = manifest_digest(
            {"schema_version": "snapshot_member_provenance.v1", **identity_values}
        )
        if self.provenance_digest != expected_provenance:
            raise ValueError("member provenance digest is invalid")
        return self

    def content_identity(self) -> dict[str, object]:
        """Return member identity with audit-only acquisition time removed."""
        payload = self.model_dump(mode="python", exclude={"exclusion_evidence"})
        payload["exclusion_evidence"] = (
            None
            if self.exclusion_evidence is None
            else self.exclusion_evidence.content_identity()
        )
        return payload

    @classmethod
    def valid_scan(cls, record: HistoricalScanRecordV1) -> "SnapshotMemberV1":
        values = {
            "schema_version": "snapshot_member.v1",
            "security_id": record.security_id,
            "observed_symbol": record.observed_symbol,
            "mic": record.mic,
            "as_of_session_date": record.as_of_session_date,
            "resolution": "valid_scan",
            "source_cutoff": record.as_of_session_date,
            "source_payload_digest": record.digest(),
            "input_revision": record.provenance.input_revision,
            "provider_data_revision": record.provenance.provider_data_revision,
            "provider_evidence_manifest_digest": (
                record.provenance.provider_evidence_manifest_digest
            ),
            "alias_revision": record.provenance.alias_revision,
            "record_digest": record.digest(),
            "exclusion_reason": None,
            "exclusion_evidence": None,
        }
        values["provenance_digest"] = manifest_digest(
            {"schema_version": "snapshot_member_provenance.v1", **values}
        )
        return cls.model_validate(values)

    @classmethod
    def legitimate_exclusion(
        cls, proof: LegitimateExclusionProofV1
    ) -> "SnapshotMemberV1":
        values = {
            "schema_version": "snapshot_member.v1",
            "security_id": proof.security_id,
            "observed_symbol": proof.observed_symbol,
            "mic": proof.mic,
            "as_of_session_date": proof.target_session,
            "resolution": "legitimate_exclusion",
            "source_cutoff": proof.target_session,
            "source_payload_digest": proof.evidence_revision,
            "input_revision": proof.content_digest,
            "provider_data_revision": proof.evidence_revision,
            "provider_evidence_manifest_digest": proof.evidence_manifest_digest,
            "alias_revision": proof.alias_revision,
            "record_digest": None,
            "exclusion_reason": proof.exclusion_reason,
            "exclusion_evidence": proof,
        }
        identity_values: dict[str, object] = dict(values)
        identity_values["exclusion_evidence"] = proof.content_identity()
        values["provenance_digest"] = manifest_digest(
            {"schema_version": "snapshot_member_provenance.v1", **identity_values}
        )
        return cls.model_validate(values)


class SnapshotMonthManifestV1(CanonicalModel):
    schema_version: Literal["snapshot_month_manifest.v1"]
    profile_hash: Digest
    snapshot_month: SnapshotMonth
    provenance_quality: ProvenanceQuality
    processing_complete: Literal[True]
    market_complete: Literal["unknown"]
    roster_digest: Digest
    expected_digest: Digest
    input_revision_digest: Digest
    result_digest: Digest
    expected_count: Annotated[int, Field(ge=0)]
    valid_count: Annotated[int, Field(ge=0)]
    excluded_count: Annotated[int, Field(ge=0)]
    content_digest: Digest
    source_run_id: NonEmpty | None
    observed_at: datetime | None
    committed_at: datetime

    @field_validator("committed_at", "observed_at")
    @classmethod
    def _utc_instants(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("snapshot timestamps must be UTC instants")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _balanced_counts(self) -> "SnapshotMonthManifestV1":
        if self.expected_count != self.valid_count + self.excluded_count:
            raise ValueError("snapshot counts do not balance")
        if self.provenance_quality == "best_effort_reconstructed" and (
            self.source_run_id is not None or self.observed_at is not None
        ):
            raise ValueError("reconstructed month cannot claim observed BAU evidence")
        if self.provenance_quality == "observed_bau" and (
            self.source_run_id is None or self.observed_at is None
        ):
            raise ValueError("observed BAU month requires source evidence")
        return self

    @property
    def semantic_content_digest(self) -> str:
        """Content identity excluding run/time audit metadata."""
        return manifest_digest(
            {
                "schema_version": "snapshot_month_semantic_content.v1",
                "profile_hash": self.profile_hash,
                "snapshot_month": self.snapshot_month,
                "provenance_quality": self.provenance_quality,
                "roster_digest": self.roster_digest,
                "expected_digest": self.expected_digest,
                "input_revision_digest": self.input_revision_digest,
                "result_digest": self.result_digest,
                "expected_count": self.expected_count,
                "valid_count": self.valid_count,
                "excluded_count": self.excluded_count,
            }
        )


class MonthlySnapshotCommitV1(CanonicalModel):
    schema_version: Literal["monthly_snapshot_commit.v1"]
    validated_as_of: date
    profile: SnapshotProfileV1
    profile_hash: Digest
    manifest: SnapshotMonthManifestV1
    members: tuple[SnapshotMemberV1, ...]
    records: tuple[HistoricalScanRecordV1, ...]

    @model_validator(mode="after")
    def _complete_write_set(self) -> "MonthlySnapshotCommitV1":
        if self.profile_hash != self.profile.profile_hash:
            raise ValueError("profile hash does not match profile")
        if self.manifest.profile_hash != self.profile_hash:
            raise ValueError("month profile does not match commit profile")
        try:
            TradingCalendar.closed_month(
                self.manifest.snapshot_month, as_of=self.validated_as_of
            )
        except ValueError as exc:
            raise SnapshotContractError(
                "snapshot month is not fully closed", code="calendar_error"
            ) from exc
        self._validate_members_and_records(
            self.profile,
            self.manifest.snapshot_month,
            self.manifest.provenance_quality,
            self.members,
            self.records,
        )
        expected = self._manifest(
            profile=self.profile,
            snapshot_month=self.manifest.snapshot_month,
            provenance_quality=self.manifest.provenance_quality,
            members=self.members,
            records=self.records,
            committed_at=self.manifest.committed_at,
            source_run_id=self.manifest.source_run_id,
            observed_at=self.manifest.observed_at,
        )
        if expected != self.manifest:
            raise ValueError("month manifest does not match canonical write set")
        return self

    @classmethod
    def build(
        cls,
        *,
        profile: SnapshotProfileV1,
        snapshot_month: str,
        provenance_quality: ProvenanceQuality,
        members: tuple[SnapshotMemberV1, ...],
        records: tuple[HistoricalScanRecordV1, ...],
        committed_at: datetime,
        as_of: date,
        source_run_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> "MonthlySnapshotCommitV1":
        sorted_members = tuple(sorted(members, key=lambda item: item.security_id))
        sorted_records = tuple(sorted(records, key=lambda item: item.security_id))
        try:
            TradingCalendar.closed_month(snapshot_month, as_of=as_of)
            cls._validate_members_and_records(
                profile,
                snapshot_month,
                provenance_quality,
                sorted_members,
                sorted_records,
            )
            manifest = cls._manifest(
                profile=profile,
                snapshot_month=snapshot_month,
                provenance_quality=provenance_quality,
                members=sorted_members,
                records=sorted_records,
                committed_at=committed_at,
                source_run_id=source_run_id,
                observed_at=observed_at,
            )
            return cls(
                schema_version="monthly_snapshot_commit.v1",
                validated_as_of=as_of,
                profile=profile,
                profile_hash=profile.profile_hash,
                manifest=manifest,
                members=sorted_members,
                records=sorted_records,
            )
        except SnapshotContractError:
            raise
        except ValueError as exc:
            if getattr(exc, "code", None) == "calendar_error":
                raise SnapshotContractError(
                    "snapshot month is not fully closed", code="calendar_error"
                ) from exc
            raise SnapshotContractError(
                "monthly snapshot write set is invalid"
            ) from exc
        except Exception as exc:
            raise SnapshotContractError(
                "monthly snapshot write set is invalid"
            ) from exc

    @staticmethod
    def _validate_members_and_records(
        profile: SnapshotProfileV1,
        snapshot_month: str,
        provenance_quality: ProvenanceQuality,
        members: tuple[SnapshotMemberV1, ...],
        records: tuple[HistoricalScanRecordV1, ...],
    ) -> None:
        member_ids = tuple(item.security_id for item in members)
        record_ids = tuple(item.security_id for item in records)
        if len(set(member_ids)) != len(member_ids):
            raise SnapshotContractError("duplicate member in monthly snapshot")
        if len(set(record_ids)) != len(record_ids):
            raise SnapshotContractError("duplicate record in monthly snapshot")
        valid_ids = {
            item.security_id for item in members if item.resolution == "valid_scan"
        }
        if valid_ids != set(record_ids):
            raise SnapshotContractError("valid member requires one record")
        records_by_id = {item.security_id: item for item in records}
        calendar = TradingCalendar()
        for member in members:
            if member.as_of_session_date.strftime("%Y-%m") != snapshot_month:
                raise SnapshotContractError("member session is outside snapshot month")
            try:
                expected_session = calendar.last_session_of_month(
                    member.mic, snapshot_month
                )
            except ValueError as exc:
                raise SnapshotContractError(
                    "member calendar is unsupported", code="calendar_error"
                ) from exc
            if member.as_of_session_date != expected_session:
                raise SnapshotContractError(
                    "member session is not the canonical month end",
                    code="calendar_error",
                )
            if member.resolution == "legitimate_exclusion":
                if provenance_quality != "best_effort_reconstructed":
                    raise SnapshotContractError(
                        "observed BAU cannot contain exclusions"
                    )
                proof = member.exclusion_evidence
                assert proof is not None
                if member != SnapshotMemberV1.legitimate_exclusion(proof):
                    raise SnapshotContractError(
                        "excluded member evidence is not canonical"
                    )
                if (
                    proof.calendar_dataset_version != profile.calendar_dataset_version
                    or proof.calendar_dataset_digest != profile.calendar_dataset_digest
                    or proof.request_contract_version
                    != profile.yfinance_request_contract_version
                ):
                    raise SnapshotContractError(
                        "exclusion proof does not match snapshot profile"
                    )
                continue
            record = records_by_id[member.security_id]
            if member != SnapshotMemberV1.valid_scan(record):
                raise SnapshotContractError("valid member evidence is not canonical")
            if (
                record.snapshot_month != snapshot_month
                or record.provenance_quality != provenance_quality
                or record.observed_symbol != member.observed_symbol
                or record.mic != member.mic
                or record.as_of_session_date != member.as_of_session_date
                or record.digest() != member.record_digest
                or record.provenance.input_revision != member.input_revision
                or record.provenance.provider_data_revision
                != member.provider_data_revision
                or record.provenance.provider_evidence_manifest_digest
                != member.provider_evidence_manifest_digest
                or record.provenance.roster_digest != profile.roster_digest
                or record.provenance.calendar_dataset_version
                != profile.calendar_dataset_version
                or record.provenance.calendar_dataset_digest
                != profile.calendar_dataset_digest
                or record.provenance.detector_versions != profile.detector_versions
                or record.provenance.provider_request_contract_version
                != profile.yfinance_request_contract_version
                or record.provenance.yfinance_ingestion_version
                != profile.yfinance_ingestion_version
            ):
                raise SnapshotContractError(
                    "record identity does not match profile/member"
                )

    @staticmethod
    def _manifest(
        *,
        profile: SnapshotProfileV1,
        snapshot_month: str,
        provenance_quality: ProvenanceQuality,
        members: tuple[SnapshotMemberV1, ...],
        records: tuple[HistoricalScanRecordV1, ...],
        committed_at: datetime,
        source_run_id: str | None,
        observed_at: datetime | None,
    ) -> SnapshotMonthManifestV1:
        expected_digest = manifest_digest(
            {
                "schema_version": "snapshot_expected_members.v1",
                "roster_digest": profile.roster_digest,
                "members": [
                    {
                        "security_id": item.security_id,
                        "observed_symbol": item.observed_symbol,
                        "mic": item.mic,
                        "as_of_session_date": item.as_of_session_date,
                    }
                    for item in members
                ],
            }
        )
        input_revision_digest = manifest_digest(
            {
                "schema_version": "snapshot_input_revisions.v1",
                "inputs": [
                    {
                        "security_id": item.security_id,
                        "input_revision": item.input_revision,
                        "provider_data_revision": item.provider_data_revision,
                        "provider_evidence_manifest_digest": (
                            item.provider_evidence_manifest_digest
                        ),
                        "provenance_digest": item.provenance_digest,
                    }
                    for item in members
                ],
            }
        )
        result_digest = manifest_digest(
            {
                "schema_version": "snapshot_results.v1",
                "results": [
                    {"security_id": item.security_id, "record_digest": item.digest()}
                    for item in records
                ],
            }
        )
        content_digest = manifest_digest(
            {
                "schema_version": "snapshot_month_content.v1",
                "profile_hash": profile.profile_hash,
                "snapshot_month": snapshot_month,
                "provenance_quality": provenance_quality,
                "roster_digest": profile.roster_digest,
                "expected_digest": expected_digest,
                "input_revision_digest": input_revision_digest,
                "result_digest": result_digest,
                "source_run_id": source_run_id,
                "observed_at": observed_at,
                "members": [item.content_identity() for item in members],
            }
        )
        excluded_count = sum(
            item.resolution == "legitimate_exclusion" for item in members
        )
        return SnapshotMonthManifestV1(
            schema_version="snapshot_month_manifest.v1",
            profile_hash=profile.profile_hash,
            snapshot_month=snapshot_month,
            provenance_quality=provenance_quality,
            processing_complete=True,
            market_complete="unknown",
            roster_digest=profile.roster_digest,
            expected_digest=expected_digest,
            input_revision_digest=input_revision_digest,
            result_digest=result_digest,
            expected_count=len(members),
            valid_count=len(records),
            excluded_count=excluded_count,
            content_digest=content_digest,
            source_run_id=source_run_id,
            observed_at=observed_at,
            committed_at=committed_at,
        )


class CoverageIntervalV1(CanonicalModel):
    start_month: SnapshotMonth
    end_month: SnapshotMonth


class ProvenanceCoverageV1(CanonicalModel):
    provenance_quality: ProvenanceQuality
    snapshot_count: Annotated[int, Field(ge=0)]
    intervals: tuple[CoverageIntervalV1, ...]


class CoverageSummaryV1(CanonicalModel):
    profile_hash: Digest
    display_version: NonEmpty
    earliest_month: SnapshotMonth | None
    latest_month: SnapshotMonth | None
    snapshot_count: Annotated[int, Field(ge=0)]
    intervals: tuple[CoverageIntervalV1, ...]
    provenance: tuple[ProvenanceCoverageV1, ...]


class IntervalReadinessV1(CanonicalModel):
    profile_hash: Digest
    start_month: SnapshotMonth
    end_month: SnapshotMonth
    ready: bool
    no_op: bool
    missing_months: tuple[SnapshotMonth, ...]
    ordered_month_digest: Digest | None

    @model_validator(mode="after")
    def _consistent_state(self) -> "IntervalReadinessV1":
        if self.ready != self.no_op:
            raise ValueError("ready duplicate interval must be a no-op")
        if self.ready and (self.missing_months or self.ordered_month_digest is None):
            raise ValueError("ready interval evidence is incomplete")
        if not self.ready and (
            not self.missing_months or self.ordered_month_digest is not None
        ):
            raise ValueError("non-ready interval evidence is inconsistent")
        return self


class ActiveSnapshotProfileV1(CanonicalModel):
    profile_hash: Digest
    activation_seq: Annotated[int, Field(gt=0)]
    activated_at: datetime

    @field_validator("activated_at")
    @classmethod
    def _utc_activated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("activation timestamp must be a UTC instant")
        return value.astimezone(timezone.utc)


def verified_evidence_manifest(
    evidence: HistoricalEvidenceV1,
) -> dict[str, object]:
    try:
        payload = json.loads(evidence.canonical_manifest_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SnapshotContractError("provider evidence manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise SnapshotContractError("provider evidence manifest is invalid")
    if (
        canonical_json_digest(evidence.canonical_manifest_json)
        != evidence.data_revision
        or payload.get("provider") != evidence.provider
        or payload.get("provider_version") != evidence.provider_version
        or payload.get("request_contract_version") != evidence.request_contract_version
        or payload.get("security_id") != evidence.security_id
        or payload.get("requested_symbol") != evidence.requested_symbol
        or payload.get("observed_symbol") != evidence.observed_symbol
        or payload.get("alias_revision") != evidence.alias_revision
        or payload.get("request") != dict(evidence.request_contract)
        or payload.get("currency") != evidence.currency
        or payload.get("quote_unit") != evidence.quote_unit
        or payload.get("quote_unit_scale") != evidence.quote_unit_scale
        or payload.get("exchange_timezone") != evidence.exchange_timezone
        or payload.get("rows") != list(evidence.rows)
        or payload.get("actions") != list(evidence.actions)
    ):
        raise SnapshotContractError("provider evidence integrity failure")
    return cast(dict[str, object], payload)


def _validated_observation_sessions(
    evidence: HistoricalEvidenceV1,
    *,
    mic: Literal["BATS", "XNAS", "XNYS", "XLON"],
) -> tuple[date, ...]:
    calendar = TradingCalendar()
    expected_keys = {
        "session",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "dividends",
        "stock_splits",
    }
    try:
        start = date.fromisoformat(evidence.start)
        end = date.fromisoformat(evidence.end)
        sessions: list[date] = []
        for row in evidence.rows:
            if set(row) != expected_keys:
                raise ValueError("observation fields are incomplete")
            session = date.fromisoformat(str(row["session"]))
            if (
                session < start
                or session >= end
                or not calendar.is_session(mic, session)
            ):
                raise ValueError(
                    "observation is outside the canonical request sessions"
                )
            numbers: dict[str, float] = {}
            for name in expected_keys - {"session", "adj_close"}:
                rendered = row[name]
                if not isinstance(rendered, str):
                    raise ValueError("observation value is not canonical")
                number = float.fromhex(rendered)
                if not math.isfinite(number) or number.hex() != rendered:
                    raise ValueError("observation value is not canonical")
                numbers[name] = number
            adjusted = row["adj_close"]
            if adjusted is not None:
                if not isinstance(adjusted, str):
                    raise ValueError("adjusted close is not canonical")
                adjusted_number = float.fromhex(adjusted)
                if (
                    not math.isfinite(adjusted_number)
                    or adjusted_number.hex() != adjusted
                ):
                    raise ValueError("adjusted close is not canonical")
            if any(
                numbers[name] < 0 for name in expected_keys - {"session", "adj_close"}
            ):
                raise ValueError("observation values cannot be negative")
            if numbers["high"] < numbers["low"]:
                raise ValueError("observation high/low range is invalid")
            sessions.append(session)
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotContractError("full-history observations are malformed") from exc
    if tuple(sessions) != tuple(sorted(set(sessions))):
        raise SnapshotContractError("full-history observations are not canonical")
    return tuple(sessions)


def build_before_first_provider_observation(
    *,
    evidence: HistoricalEvidenceV1,
    snapshot_month: str,
    target_session: date,
    mic: Literal["BATS", "XNAS", "XNYS", "XLON"],
    alias_revision: str,
    alias_effective_from: date | None,
    alias_effective_to: date | None,
    calendar_dataset_version: str,
    calendar_dataset_digest: str,
    acquired_at: datetime,
) -> LegitimateExclusionProofV1:
    """Build the sole legal exclusion from verified immutable full-history evidence."""
    verified_evidence_manifest(evidence)
    calendar = TradingCalendar()
    if calendar_dataset_version != "exchange-calendars-v1" or (
        calendar_dataset_digest != calendar.session_table_digest()
    ):
        raise SnapshotContractError("calendar evidence does not match authority")
    if target_session != calendar.last_session_of_month(mic, snapshot_month):
        raise SnapshotContractError("target session does not match canonical month")
    if evidence.alias_revision != alias_revision:
        raise SnapshotContractError("alias revision does not match evidence")
    expected_currency = "USD" if mic in {"BATS", "XNAS", "XNYS"} else "GBP"
    expected_units = {"USD"} if expected_currency == "USD" else {"GBP", "GBp"}
    if (
        evidence.currency != expected_currency
        or evidence.quote_unit not in expected_units
    ):
        raise SnapshotContractError("currency/unit does not match MIC")
    try:
        start = date.fromisoformat(evidence.start)
        end = date.fromisoformat(evidence.end)
    except ValueError as exc:
        raise SnapshotContractError("full-history bounds are malformed") from exc
    if start != FULL_HISTORY_START:
        raise SnapshotContractError(
            "before-first exclusion requires a canonical full-history request",
            code="required_data_missing",
        )
    if evidence.request_contract.get("start") != evidence.start or (
        evidence.request_contract.get("end") != evidence.end
    ):
        raise SnapshotContractError("full-history request contract is inconsistent")
    valid_sessions = _validated_observation_sessions(evidence, mic=mic)
    if not valid_sessions:
        raise SnapshotContractError(
            "full-history evidence has no valid observation",
            code="required_data_missing",
        )
    try:
        first_observed = min(valid_sessions)
    except ValueError as exc:
        raise SnapshotContractError("full-history observations are malformed") from exc
    if target_session >= first_observed:
        raise SnapshotContractError(
            "target session must precede first provider observation",
            code="required_data_missing",
        )
    if end <= first_observed:
        raise SnapshotContractError("full-history evidence excludes first observation")
    try:
        return LegitimateExclusionProofV1(
            schema_version="before_first_provider_observation.v1",
            exclusion_reason="before_first_provider_observation",
            security_id=evidence.security_id,
            requested_symbol=evidence.requested_symbol,
            observed_symbol=evidence.observed_symbol,
            alias_revision=alias_revision,
            alias_effective_from=alias_effective_from,
            alias_effective_to=alias_effective_to,
            snapshot_month=snapshot_month,
            mic=mic,
            target_session=target_session,
            calendar_dataset_version="exchange-calendars-v1",
            calendar_dataset_digest=calendar_dataset_digest,
            provider="yfinance",
            provider_version=evidence.provider_version,
            request_contract_version="YFinanceDailyProviderNativeV1",
            full_history_start=start,
            full_history_end_exclusive=end,
            evidence_revision=evidence.data_revision,
            evidence_manifest_digest=evidence.data_revision,
            first_observed_session=first_observed,
            currency=cast(Literal["USD", "GBP"], evidence.currency),
            quote_unit=cast(Literal["USD", "GBP", "GBp"], evidence.quote_unit),
            acquired_at=acquired_at,
            provider_observed_lifetime_only=True,
            verified_listing_date=False,
        )
    except Exception as exc:
        raise SnapshotContractError("before-first exclusion proof is invalid") from exc


def build_insufficient_detector_history(
    *,
    evidence: HistoricalEvidenceV1,
    snapshot_month: str,
    target_session: date,
    mic: Literal["BATS", "XNAS", "XNYS", "XLON"],
    alias_revision: str,
    alias_effective_from: date | None,
    alias_effective_to: date | None,
    calendar_dataset_version: str,
    calendar_dataset_digest: str,
    acquired_at: datetime,
) -> LegitimateExclusionProofV1:
    """Prove that a listed member has not accumulated 252 usable sessions yet."""
    verified_evidence_manifest(evidence)
    calendar = TradingCalendar()
    if calendar_dataset_version != "exchange-calendars-v1" or (
        calendar_dataset_digest != calendar.session_table_digest()
    ):
        raise SnapshotContractError("calendar evidence does not match authority")
    if target_session != calendar.last_session_of_month(mic, snapshot_month):
        raise SnapshotContractError("target session does not match canonical month")
    if evidence.alias_revision != alias_revision:
        raise SnapshotContractError("alias revision does not match evidence")
    expected_currency = "USD" if mic in {"BATS", "XNAS", "XNYS"} else "GBP"
    expected_units = {"USD"} if expected_currency == "USD" else {"GBP", "GBp"}
    if (
        evidence.currency != expected_currency
        or evidence.quote_unit not in expected_units
    ):
        raise SnapshotContractError("currency/unit does not match MIC")
    try:
        start = date.fromisoformat(evidence.start)
        end = date.fromisoformat(evidence.end)
    except ValueError as exc:
        raise SnapshotContractError("full-history bounds are malformed") from exc
    if start != FULL_HISTORY_START:
        raise SnapshotContractError(
            "insufficient-history exclusion requires a full-history request",
            code="required_data_missing",
        )
    if evidence.request_contract.get("start") != evidence.start or (
        evidence.request_contract.get("end") != evidence.end
    ):
        raise SnapshotContractError("full-history request contract is inconsistent")
    observed = tuple(
        session
        for session in _validated_observation_sessions(evidence, mic=mic)
        if session <= target_session
    )
    if not observed or observed[-1] != target_session or len(observed) >= 252:
        raise SnapshotContractError(
            "target does not have bounded insufficient history",
            code="required_data_missing",
        )
    expected = calendar.sessions_in_range(
        mic, observed[0], target_session + timedelta(days=1)
    )
    if observed != expected:
        raise SnapshotContractError(
            "available detector history contains a source gap",
            code="required_data_missing",
        )
    try:
        return LegitimateExclusionProofV1(
            schema_version="insufficient_detector_history.v1",
            exclusion_reason="insufficient_detector_history",
            security_id=evidence.security_id,
            requested_symbol=evidence.requested_symbol,
            observed_symbol=evidence.observed_symbol,
            alias_revision=alias_revision,
            alias_effective_from=alias_effective_from,
            alias_effective_to=alias_effective_to,
            snapshot_month=snapshot_month,
            mic=mic,
            target_session=target_session,
            calendar_dataset_version="exchange-calendars-v1",
            calendar_dataset_digest=calendar_dataset_digest,
            provider="yfinance",
            provider_version=evidence.provider_version,
            request_contract_version="YFinanceDailyProviderNativeV1",
            full_history_start=start,
            full_history_end_exclusive=end,
            evidence_revision=evidence.data_revision,
            evidence_manifest_digest=evidence.data_revision,
            first_observed_session=observed[0],
            currency=cast(Literal["USD", "GBP"], evidence.currency),
            quote_unit=cast(Literal["USD", "GBP", "GBp"], evidence.quote_unit),
            acquired_at=acquired_at,
            provider_observed_lifetime_only=True,
            verified_listing_date=False,
            required_history_sessions=252,
            available_history_sessions=len(observed),
        )
    except Exception as exc:
        raise SnapshotContractError(
            "insufficient-history exclusion proof is invalid"
        ) from exc


def build_incomplete_detector_history(
    *,
    evidence: HistoricalEvidenceV1,
    snapshot_month: str,
    target_session: date,
    mic: Literal["BATS", "XNAS", "XNYS", "XLON"],
    alias_revision: str,
    alias_effective_from: date | None,
    alias_effective_to: date | None,
    calendar_dataset_version: str,
    calendar_dataset_digest: str,
    acquired_at: datetime,
) -> LegitimateExclusionProofV1:
    """Prove a source gap inside the detector's required 252-session window."""
    verified_evidence_manifest(evidence)
    calendar = TradingCalendar()
    if calendar_dataset_version != "exchange-calendars-v1" or (
        calendar_dataset_digest != calendar.session_table_digest()
    ):
        raise SnapshotContractError("calendar evidence does not match authority")
    if target_session != calendar.last_session_of_month(mic, snapshot_month):
        raise SnapshotContractError("target session does not match canonical month")
    if evidence.alias_revision != alias_revision:
        raise SnapshotContractError("alias revision does not match evidence")
    expected_currency = "USD" if mic in {"BATS", "XNAS", "XNYS"} else "GBP"
    expected_units = {"USD"} if expected_currency == "USD" else {"GBP", "GBp"}
    if (
        evidence.currency != expected_currency
        or evidence.quote_unit not in expected_units
    ):
        raise SnapshotContractError("currency/unit does not match MIC")
    try:
        start = date.fromisoformat(evidence.start)
        end = date.fromisoformat(evidence.end)
    except ValueError as exc:
        raise SnapshotContractError("full-history bounds are malformed") from exc
    if start != FULL_HISTORY_START:
        raise SnapshotContractError(
            "incomplete-history exclusion requires a full-history request",
            code="required_data_missing",
        )
    if evidence.request_contract.get("start") != evidence.start or (
        evidence.request_contract.get("end") != evidence.end
    ):
        raise SnapshotContractError("full-history request contract is inconsistent")
    observed = _validated_observation_sessions(evidence, mic=mic)
    if not observed or observed[0] > target_session:
        raise SnapshotContractError(
            "incomplete-history exclusion requires an earlier observation",
            code="required_data_missing",
        )
    expected_window = calendar.sessions_in_range(
        mic, FULL_HISTORY_START, target_session + timedelta(days=1)
    )[-252:]
    observed_set = set(observed)
    missing = tuple(
        session for session in expected_window if session not in observed_set
    )
    if not missing:
        raise SnapshotContractError("detector window is not incomplete")
    available = len(expected_window) - len(missing)
    missing_digest = manifest_digest(
        {
            "schema_version": "missing_detector_sessions.v1",
            "sessions": tuple(session.isoformat() for session in missing),
        }
    )
    try:
        return LegitimateExclusionProofV1(
            schema_version="incomplete_detector_history.v1",
            exclusion_reason="incomplete_detector_history",
            security_id=evidence.security_id,
            requested_symbol=evidence.requested_symbol,
            observed_symbol=evidence.observed_symbol,
            alias_revision=alias_revision,
            alias_effective_from=alias_effective_from,
            alias_effective_to=alias_effective_to,
            snapshot_month=snapshot_month,
            mic=mic,
            target_session=target_session,
            calendar_dataset_version="exchange-calendars-v1",
            calendar_dataset_digest=calendar_dataset_digest,
            provider="yfinance",
            provider_version=evidence.provider_version,
            request_contract_version="YFinanceDailyProviderNativeV1",
            full_history_start=start,
            full_history_end_exclusive=end,
            evidence_revision=evidence.data_revision,
            evidence_manifest_digest=evidence.data_revision,
            first_observed_session=observed[0],
            currency=cast(Literal["USD", "GBP"], evidence.currency),
            quote_unit=cast(Literal["USD", "GBP", "GBp"], evidence.quote_unit),
            acquired_at=acquired_at,
            provider_observed_lifetime_only=True,
            verified_listing_date=False,
            required_history_sessions=252,
            available_history_sessions=available,
            missing_session_digest=missing_digest,
        )
    except Exception as exc:
        raise SnapshotContractError(
            "incomplete-history exclusion proof is invalid"
        ) from exc


__all__ = [
    "ActiveSnapshotProfileV1",
    "CoverageIntervalV1",
    "CoverageSummaryV1",
    "HistoricalEvidenceV1",
    "IntervalReadinessV1",
    "LegitimateExclusionProofV1",
    "MonthlySnapshotCommitV1",
    "ProfileDetectorV1",
    "ProvenanceCoverageV1",
    "SnapshotContractError",
    "SnapshotMemberV1",
    "SnapshotMonthManifestV1",
    "SnapshotProfileV1",
    "build_before_first_provider_observation",
    "build_insufficient_detector_history",
    "build_incomplete_detector_history",
    "verified_evidence_manifest",
]
