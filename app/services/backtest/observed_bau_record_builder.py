"""Build observed BAU records from scanner-owned provider evidence only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.bau_run_envelope import BauCaptureMemberV1
from app.services.backtest.detectors import (
    required_history_sessions,
    run_detector_suite,
)
from app.services.backtest.historical_scan_record import (
    EnrichmentV1,
    HistoricalScanRecordV1,
    ProvenanceV1,
    DetectorFragmentEnvelopeV1,
)
from app.services.backtest.market_planes import HistoricalMarketPlanes
from app.services.backtest.trading_calendar import TradingCalendar


class ObservedBauBuildError(ValueError):
    """The exact scanner-owned evidence cannot prove one observed member."""


@dataclass(frozen=True)
class ObservedBauBuildResult:
    record: HistoricalScanRecordV1
    fragments: tuple[DetectorFragmentEnvelopeV1, ...]


class ObservedBauRecordBuilder:
    """A deliberately separate path from ``HistoricalScanReconstructor``.

    It accepts only a capture member: there is no historical request, cache, or
    provider adapter argument that could fetch data after the scanner run.
    """

    def build(
        self, member: BauCaptureMemberV1, *, roster_captured_at
    ) -> ObservedBauBuildResult:
        raw = member.raw_evidence
        evidence = StoredHistoricalEvidence(
            data_revision=raw.data_revision,
            security_id=raw.security_id,
            provider=raw.provider,
            provider_version=raw.provider_version,
            request_contract_version=raw.request_contract_version,
            requested_symbol=raw.requested_symbol,
            observed_symbol=raw.observed_symbol,
            alias_revision=raw.alias_revision,
            currency=raw.currency,
            quote_unit=raw.quote_unit,
            quote_unit_scale=raw.quote_unit_scale,
            exchange_timezone=raw.exchange_timezone,
            start=raw.start.isoformat(),
            end=raw.end.isoformat(),
            request_contract=raw.request_contract,
            response_metadata_digest=raw.response_metadata_digest,
            canonical_manifest_json=raw.canonical_manifest_json,
            rows=raw.rows,
            actions=raw.actions,
        )
        try:
            planes = HistoricalMarketPlanes.from_evidence(evidence)
            bounded = planes.split_continuous_as_of(member.canonical_session)
        except Exception as exc:
            raise ObservedBauBuildError("raw BAU evidence is malformed") from exc
        required = required_history_sessions()
        rows = tuple(bounded[-required:])
        expected = tuple(
            stamp.date()
            for stamp in TradingCalendar()
            ._calendar(member.mic)
            .sessions_window(member.canonical_session, -required)
        )
        if len(rows) != required or tuple(row.session for row in rows) != expected:
            raise ObservedBauBuildError(
                "raw BAU evidence lacks exact canonical history"
            )

        manifest = member.input_manifest
        try:
            suite = run_detector_suite(
                rows,
                security_id=member.security_id,
                as_of_session=member.canonical_session,
                detector_versions=manifest.detector_versions,
                input_revision=manifest.digest(),
            )
        except Exception as exc:
            raise ObservedBauBuildError("BAU detector failed on raw evidence") from exc
        record = HistoricalScanRecordV1(
            schema_version="historical_scan_record.v1",
            security_id=member.security_id,
            observed_symbol=raw.observed_symbol,
            mic=member.mic,
            snapshot_month=member.canonical_session.strftime("%Y-%m"),
            as_of_session_date=member.canonical_session,
            currency=cast(Literal["USD", "GBP"], planes.currency),
            quote_unit=cast(Literal["USD", "GBP", "GBp"], planes.quote_unit),
            provenance_quality="observed_bau",
            technicals=suite.technicals,
            stage=suite.stage,
            vcp=suite.vcp,
            enrichment=EnrichmentV1(),
            provenance=ProvenanceV1(
                price_provider="yfinance",
                universe_basis="captured_configured_roster",
                roster_captured_at=roster_captured_at,
                point_in_time_universe=False,
                survivorship_bias="known",
                renamed_or_delisted_may_be_absent=True,
                historical_tradingview_screen_available=False,
                roster_digest=manifest.roster_digest,
                alias_revision=manifest.alias_revision,
                calendar_dataset_version=manifest.calendar_dataset_version,
                calendar_dataset_digest=manifest.calendar_dataset_digest,
                provider_evidence_manifest_digest=raw.data_revision,
                provider_data_revision=raw.data_revision,
                provider_request_contract_version=raw.request_contract_version,
                yfinance_ingestion_version=manifest.yfinance_ingestion_version,
                input_revision=manifest.digest(),
                detector_versions=manifest.detector_versions,
            ),
        )
        return ObservedBauBuildResult(record=record, fragments=suite.fragments)


__all__ = [
    "ObservedBauBuildError",
    "ObservedBauBuildResult",
    "ObservedBauRecordBuilder",
]
