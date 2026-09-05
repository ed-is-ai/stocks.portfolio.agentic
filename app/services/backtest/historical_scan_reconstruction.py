"""Deterministic bounded reconstruction of canonical historical scan records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
import json
from pathlib import Path

from pydantic import ValidationError

from app.repositories.backtest_repo import (
    BacktestIntegrityError,
    BacktestRepository,
    DetectorCacheKey,
)
from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.canonical_manifest import (
    canonical_json,
    canonical_json_digest,
    manifest_digest,
)
from app.services.backtest.detectors import (
    DETECTOR_REGISTRY,
    DetectorContext,
    DetectorExecutionError,
    required_history_sessions,
)
from app.services.backtest.historical_scan_record import (
    DetectorFragmentEnvelopeV1,
    EnrichmentV1,
    HistoricalScanContractError,
    HistoricalScanRecordV1,
    ProvenanceV1,
    StageResultV1,
    StageV1,
    TechnicalResultV1,
    TechnicalsV1,
    VcpResultV1,
    VcpV1,
)
from app.services.backtest.market_planes import HistoricalMarketPlanes
from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
from app.services.backtest.reconstruction_roster import (
    CapturedRosterMemberV1,
    CapturedRosterV1,
)
from app.services.backtest.source_manifest import (
    ReconstructionInputManifestV1,
    detector_source_manifests,
    yfinance_ingestion_source_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar


CALENDAR_DATASET_VERSION = "exchange-calendars-v1"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TRADING_CALENDAR = TradingCalendar()


@lru_cache(maxsize=1)
def canonical_calendar_digest() -> str:
    return _TRADING_CALENDAR.session_table_digest()


@lru_cache(maxsize=None)
def _required_calendar_sessions(
    mic: str, as_of_session_date: date, count: int
) -> tuple[date, ...]:
    calendar = _TRADING_CALENDAR._calendar(mic)
    sessions = calendar.sessions_window(as_of_session_date, -count)
    return tuple(timestamp.date() for timestamp in sessions)


class ReconstructionError(ValueError):
    """Stable typed reconstruction failure with deterministic context."""

    def __init__(
        self,
        code: str,
        *,
        security_id: str,
        as_of_session_date: date,
        detector: str | None = None,
        detail: str,
    ) -> None:
        self.code = code
        self.security_id = security_id
        self.as_of_session_date = as_of_session_date
        self.detector = detector
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class ReconstructionRequestV1:
    security_id: str
    observed_symbol: str
    mic: str
    snapshot_month: str
    as_of_session_date: date
    identity_candidates: tuple[str, ...]
    roster: CapturedRosterV1
    evidence: StoredHistoricalEvidence
    input_manifest: ReconstructionInputManifestV1


@dataclass(frozen=True)
class ReconstructionResultV1:
    record: HistoricalScanRecordV1
    fragments: tuple[DetectorFragmentEnvelopeV1, ...]


class HistoricalScanReconstructor:
    """Replay all supported detectors over one exact evidence revision."""

    def __init__(self, cache: BacktestRepository | None = None) -> None:
        self._cache = cache
        self._planes_cache: dict[tuple[str, str], HistoricalMarketPlanes] = {}

    def reconstruct(self, request: ReconstructionRequestV1) -> ReconstructionResultV1:
        roster_captured_at = self._validate_request(request)
        planes = self._planes_for(request)
        bounded = planes.split_continuous_as_of(request.as_of_session_date)
        required = required_history_sessions()
        if len(bounded) < required or bounded[-1].session != request.as_of_session_date:
            raise self._error(
                request,
                "required_data_missing",
                "252 completed sessions ending on the target session are required",
            )
        rows = tuple(bounded[-required:])
        if tuple(row.session for row in rows) != _required_calendar_sessions(
            request.mic, request.as_of_session_date, required
        ):
            raise self._error(
                request,
                "required_data_missing",
                "required exchange-calendar sessions are missing",
            )
        if any(row.session > request.as_of_session_date for row in rows):
            raise self._error(
                request, "integrity_error", "detector view exceeds its as-of bound"
            )

        fragments: list[DetectorFragmentEnvelopeV1] = []
        # Detector fragments are cached on cache_key_digest(), not digest():
        # roster_digest/alias_revision never reach the rows a detector
        # computes over, so two roster generations that resolve the same
        # security to the same evidence should share this cache entry
        # rather than each computing and storing their own copy.
        input_revision = request.input_manifest.cache_key_digest()
        detector_versions = request.input_manifest.detector_versions
        technicals: TechnicalsV1 | None = None
        stage_result: StageResultV1 | None = None
        vcp_result: VcpResultV1 | None = None
        for detector in DETECTOR_REGISTRY:
            key = DetectorCacheKey(
                security_id=request.security_id,
                date=request.as_of_session_date,
                detector=detector.detector_id,
                detector_version=detector_versions[detector.detector_id],
                input_revision=input_revision,
            )
            try:
                fragment = (
                    None if self._cache is None else self._cache.detector_fragment(key)
                )
                if fragment is None:
                    result = detector.run(DetectorContext(rows, technicals))
                    fragment = DetectorFragmentEnvelopeV1(
                        schema_version="scan_detector_fragment.v1",
                        security_id=request.security_id,
                        date=request.as_of_session_date,
                        detector=detector.detector_id,
                        detector_version=detector_versions[detector.detector_id],
                        detector_api_version=detector.detector_api_version,
                        input_revision=input_revision,
                        result=result,
                    )
                    if self._cache is not None:
                        fragment = self._cache.compare_and_insert_detector_fragment(
                            key, fragment.canonical_json_bytes()
                        )
                elif fragment.detector_api_version != detector.detector_api_version:
                    raise BacktestIntegrityError(
                        "cached detector API version does not match registry"
                    )
                result = fragment.result
            except DetectorExecutionError as exc:
                raise self._error(
                    request, exc.code, exc.detail, detector=exc.detector
                ) from exc
            except (KeyError, ValidationError, HistoricalScanContractError) as exc:
                raise self._error(
                    request,
                    "integrity_error",
                    "detector fragment is invalid",
                    detector=detector.detector_id,
                ) from exc
            except BacktestIntegrityError as exc:
                raise self._error(
                    request,
                    "integrity_error",
                    "detector cache integrity failure",
                    detector=detector.detector_id,
                ) from exc
            fragments.append(fragment)
            if isinstance(result, TechnicalResultV1):
                technicals = result.technicals
            elif isinstance(result, StageResultV1):
                stage_result = result
            elif isinstance(result, VcpResultV1):
                vcp_result = result

        if technicals is None or stage_result is None or vcp_result is None:
            raise self._error(
                request, "integrity_error", "detector registry is incomplete"
            )
        record = self._compose_record(
            request,
            roster_captured_at,
            planes,
            technicals,
            stage_result.stage,
            vcp_result.vcp,
        )
        return ReconstructionResultV1(record=record, fragments=tuple(fragments))

    def adopted_record(
        self,
        request: ReconstructionRequestV1,
        *,
        technicals: TechnicalsV1,
        stage: StageV1,
        vcp: VcpV1,
    ) -> HistoricalScanRecordV1:
        """Re-derive one unchanged member's record under a new input manifest.

        gh-468 Update-mode adoption: the detector payloads (technicals, stage,
        VCP) are carried verbatim from the predecessor data version's record,
        while every provenance field is recomputed exactly as ``reconstruct``
        would compute it under the new profile. Because detectors are pure
        functions of the member's own pinned evidence, the result is
        byte-identical to a from-scratch reconstruction of the same inputs.
        """
        roster_captured_at = self._validate_request(request)
        planes = self._planes_for(request)
        return self._compose_record(
            request, roster_captured_at, planes, technicals, stage, vcp
        )

    def _planes_for(self, request: ReconstructionRequestV1) -> HistoricalMarketPlanes:
        cache_key = (request.security_id, request.evidence.data_revision)
        planes = self._planes_cache.get(cache_key)
        if planes is None:
            planes = HistoricalMarketPlanes.from_evidence(request.evidence)
            self._planes_cache[cache_key] = planes
        return planes

    def _compose_record(
        self,
        request: ReconstructionRequestV1,
        roster_captured_at: datetime,
        planes: HistoricalMarketPlanes,
        technicals: TechnicalsV1,
        stage: StageV1,
        vcp: VcpV1,
    ) -> HistoricalScanRecordV1:
        input_revision = request.input_manifest.digest()
        try:
            provenance = ProvenanceV1.model_validate(
                {
                    "price_provider": "yfinance",
                    "universe_basis": "captured_configured_roster",
                    "roster_captured_at": roster_captured_at,
                    "point_in_time_universe": False,
                    "survivorship_bias": "known",
                    "renamed_or_delisted_may_be_absent": True,
                    "historical_tradingview_screen_available": False,
                    "roster_digest": request.input_manifest.roster_digest,
                    "alias_revision": request.input_manifest.alias_revision,
                    "calendar_dataset_version": (
                        request.input_manifest.calendar_dataset_version
                    ),
                    "calendar_dataset_digest": (
                        request.input_manifest.calendar_dataset_digest
                    ),
                    "provider_evidence_manifest_digest": (
                        request.input_manifest.provider_evidence_manifest_digest
                    ),
                    "provider_data_revision": request.evidence.data_revision,
                    "provider_request_contract_version": (
                        request.evidence.request_contract_version
                    ),
                    "yfinance_ingestion_version": (
                        request.input_manifest.yfinance_ingestion_version
                    ),
                    "input_revision": input_revision,
                    "detector_versions": request.input_manifest.detector_versions,
                }
            )
            record = HistoricalScanRecordV1.model_validate(
                {
                    "schema_version": "historical_scan_record.v1",
                    "security_id": request.security_id,
                    "observed_symbol": request.observed_symbol,
                    "mic": request.mic,
                    "snapshot_month": request.snapshot_month,
                    "as_of_session_date": request.as_of_session_date,
                    "currency": planes.currency,
                    "quote_unit": planes.quote_unit,
                    "provenance_quality": "best_effort_reconstructed",
                    "technicals": technicals,
                    "stage": stage,
                    "vcp": vcp,
                    "enrichment": EnrichmentV1(),
                    "provenance": provenance,
                }
            )
            # Make the model itself prove its parse/serialize authority before
            # a complete record can escape reconstruction.
            HistoricalScanRecordV1.from_canonical_json(record.canonical_json_bytes())
        except (ValidationError, HistoricalScanContractError) as exc:
            raise self._error(
                request, "integrity_error", "historical scan record is invalid"
            ) from exc
        return record

    def _validate_request(self, request: ReconstructionRequestV1) -> datetime:
        if len(request.identity_candidates) == 0:
            raise self._error(
                request, "required_data_missing", "identity evidence is missing"
            )
        if len(request.identity_candidates) != 1:
            raise self._error(
                request, "identity_ambiguous", "identity evidence is ambiguous"
            )
        if request.identity_candidates[0] != request.security_id:
            raise self._error(
                request, "integrity_error", "resolved identity does not match request"
            )
        evidence = request.evidence
        manifest = request.input_manifest
        try:
            roster_payload = json.loads(request.roster.canonical_manifest_json)
            parsed_roster = CapturedRosterV1.from_json(
                request.roster.roster_digest,
                request.roster.canonical_manifest_json,
            )
            captured_at = datetime.fromisoformat(str(roster_payload["captured_at"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise self._error(
                request, "integrity_error", "reconstruction roster is invalid"
            ) from exc
        if (
            canonical_json(roster_payload) != request.roster.canonical_manifest_json
            or manifest_digest(roster_payload) != request.roster.roster_digest
            or parsed_roster != request.roster
            or manifest.roster_digest != request.roster.roster_digest
            or roster_payload.get("alias_revision") != manifest.alias_revision
        ):
            raise self._error(
                request, "integrity_error", "reconstruction roster is invalid"
            )
        roster_members = tuple(
            member
            for member in request.roster.members
            if member.security_id == request.security_id
        )
        if len(roster_members) == 0:
            raise self._error(
                request, "required_data_missing", "roster identity evidence is missing"
            )
        if len(roster_members) != 1:
            raise self._error(
                request, "identity_ambiguous", "roster identity evidence is ambiguous"
            )
        roster_member: CapturedRosterMemberV1 = roster_members[0]
        if (
            evidence.security_id != request.security_id
            or evidence.observed_symbol != request.observed_symbol
            or evidence.alias_revision != manifest.alias_revision
            or roster_member.mic != request.mic
            or roster_member.provider_symbol != evidence.requested_symbol
            or roster_member.currency != evidence.currency
            or roster_member.quote_unit != evidence.quote_unit
        ):
            raise self._error(
                request, "integrity_error", "evidence identity does not match request"
            )
        try:
            evidence_manifest = json.loads(evidence.canonical_manifest_json)
        except json.JSONDecodeError as exc:
            raise self._error(
                request, "integrity_error", "provider evidence manifest is invalid"
            ) from exc
        if (
            canonical_json_digest(evidence.canonical_manifest_json)
            != evidence.data_revision
            or evidence_manifest.get("rows") != list(evidence.rows)
            or evidence_manifest.get("actions") != list(evidence.actions)
            or evidence_manifest.get("provider") != evidence.provider
            or evidence_manifest.get("security_id") != evidence.security_id
            or evidence_manifest.get("requested_symbol") != evidence.requested_symbol
            or evidence_manifest.get("observed_symbol") != evidence.observed_symbol
            or evidence_manifest.get("alias_revision") != evidence.alias_revision
            or evidence_manifest.get("request") != evidence.request_contract
            or evidence_manifest.get("currency") != evidence.currency
            or evidence_manifest.get("quote_unit") != evidence.quote_unit
            or evidence_manifest.get("quote_unit_scale") != evidence.quote_unit_scale
            or evidence_manifest.get("exchange_timezone") != evidence.exchange_timezone
        ):
            raise self._error(
                request, "integrity_error", "provider evidence integrity failure"
            )
        if request.snapshot_month != request.as_of_session_date.strftime("%Y-%m"):
            raise self._error(
                request, "integrity_error", "snapshot month does not contain target"
            )
        try:
            evidence_start = date.fromisoformat(evidence.start)
            evidence_end = date.fromisoformat(evidence.end)
        except ValueError as exc:
            raise self._error(
                request, "integrity_error", "evidence interval is invalid"
            ) from exc
        if (
            manifest.security_id != request.security_id
            or manifest.snapshot_month != request.snapshot_month
            or manifest.as_of_session_date != request.as_of_session_date
            or manifest.provider_data_revision != evidence.data_revision
            or manifest.evidence_start != evidence_start
            or manifest.evidence_end != evidence_end
            or manifest.provider_request_contract_version
            != evidence.request_contract_version
            or manifest.provider_evidence_manifest_digest != evidence.data_revision
            or manifest.market_plane_policy_version != PRICE_VOLUME_PLANE_VERSION
            or manifest.calendar_dataset_version != CALENDAR_DATASET_VERSION
            or manifest.calendar_dataset_digest != canonical_calendar_digest()
            or manifest.yfinance_ingestion_version
            != yfinance_ingestion_source_manifest(_PROJECT_ROOT).digest
        ):
            raise self._error(
                request, "integrity_error", "input manifest does not bind request"
            )
        manifest_detectors = {
            detector.detector_id: detector for detector in manifest.detectors
        }
        source_manifests = detector_source_manifests(_PROJECT_ROOT)
        for detector in DETECTOR_REGISTRY:
            identity = manifest_detectors[detector.detector_id]
            if (
                identity.detector_api_version != detector.detector_api_version
                or identity.detector_version
                != source_manifests[detector.detector_id].digest
                or identity.configuration != detector.configuration
            ):
                raise self._error(
                    request,
                    "integrity_error",
                    "input manifest detector identity does not match registry",
                    detector=detector.detector_id,
                )
        return captured_at

    @staticmethod
    def _error(
        request: ReconstructionRequestV1,
        code: str,
        detail: str,
        *,
        detector: str | None = None,
    ) -> ReconstructionError:
        return ReconstructionError(
            code,
            security_id=request.security_id,
            as_of_session_date=request.as_of_session_date,
            detector=detector,
            detail=detail,
        )


__all__ = [
    "HistoricalScanReconstructor",
    "ReconstructionError",
    "ReconstructionRequestV1",
    "ReconstructionResultV1",
]
