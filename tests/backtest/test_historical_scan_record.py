from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.backtest.historical_scan_record import (
    DetectorFragmentEnvelopeV1,
    EnrichmentV1,
    HistoricalScanContractError,
    HistoricalScanRecordV1,
    ProvenanceV1,
    StageV1,
    TechnicalResultV1,
    TechnicalsV1,
    VcpContractionV1,
    VcpResultV1,
    VcpV1,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
GOLDEN = Path(__file__).parent / "fixtures" / "historical_scan_record_v1.json"


def _technicals(**changes: object) -> TechnicalsV1:
    values: dict[str, object] = {
        "price": Decimal("123.4500"),
        "sma10": Decimal("122"),
        "sma30": Decimal("120"),
        "sma50": Decimal("118"),
        "sma150": Decimal("110"),
        "sma200": Decimal("100"),
        "rsi14": Decimal("61.20"),
        "atr14": Decimal("2.50"),
        "volume": Decimal("1000.12500000"),
        "vol_ma50": Decimal("800.50000000"),
        "rel_volume": Decimal("1.249999"),
        "high_52w": Decimal("130"),
        "low_52w": Decimal("80"),
        "high_base": Decimal("125"),
        "handle_low": Decimal("115"),
        "pct_from_52w_high": Decimal("-5.03846"),
        "pct_change_week": Decimal("2.5"),
    }
    values.update(changes)
    return TechnicalsV1.model_validate(values)


def _vcp() -> VcpV1:
    return VcpV1(
        valid_vcp=False,
        score=0,
        trend_template_score=Decimal("85.80"),
        trend_template_passed=False,
        wide_and_loose=False,
        breakout_volume_detected=False,
        num_contractions=0,
        contractions=(),
        pivot_price=None,
        last_contraction_low=None,
        atr_compression_ratio=None,
        right_side_range_ratio=None,
        dry_up_ratio=None,
        distance_from_pivot_pct=None,
        execution_state="Pre-breakout",
    )


def _provenance(**changes: object) -> ProvenanceV1:
    values: dict[str, object] = {
        "price_provider": "yfinance",
        "universe_basis": "captured_configured_roster",
        "roster_captured_at": datetime(2026, 8, 11, 10, 30, tzinfo=timezone.utc),
        "point_in_time_universe": False,
        "survivorship_bias": "known",
        "renamed_or_delisted_may_be_absent": True,
        "historical_tradingview_screen_available": False,
        "roster_digest": DIGEST_A,
        "alias_revision": DIGEST_B,
        "calendar_dataset_version": "exchange-calendars-v1",
        "calendar_dataset_digest": DIGEST_C,
        "provider_evidence_manifest_digest": DIGEST_A,
        "provider_data_revision": DIGEST_B,
        "provider_request_contract_version": "yfinance-daily-v1",
        "yfinance_ingestion_version": "ingestion-v1",
        "input_revision": DIGEST_C,
        "detector_versions": {
            "technical_indicators_v1": DIGEST_A,
            "weinstein_stage_v1": DIGEST_B,
            "vcp_v1": DIGEST_C,
        },
    }
    values.update(changes)
    return ProvenanceV1.model_validate(values)


def _record(**changes: object) -> HistoricalScanRecordV1:
    values: dict[str, object] = {
        "schema_version": "historical_scan_record.v1",
        "security_id": "sec-001",
        "observed_symbol": "CAFÉ",
        "mic": "XNAS",
        "snapshot_month": "2026-07",
        "as_of_session_date": date(2026, 7, 31),
        "currency": "USD",
        "quote_unit": "USD",
        "provenance_quality": "best_effort_reconstructed",
        "technicals": _technicals(),
        "stage": StageV1(value="Stage 2"),
        "vcp": _vcp(),
        "enrichment": EnrichmentV1(),
        "provenance": _provenance(),
    }
    values.update(changes)
    return HistoricalScanRecordV1.model_validate(values)


def test_record_canonical_json_is_stable_utf8_and_decimal_string_authority() -> None:
    record = _record(observed_symbol="CAFE\u0301")

    canonical = record.canonical_json_bytes()
    decoded = canonical.decode("utf-8")
    payload = json.loads(decoded)

    assert "CAFÉ" in decoded
    assert "CAFE\\u0301" not in decoded
    assert payload["technicals"]["price"] == "123.45"
    assert payload["technicals"]["volume"] == "1000.125"
    assert payload["vcp"]["trend_template_score"] == "85.8"
    assert decoded == json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert record.digest() == record.digest()
    assert HistoricalScanRecordV1.from_canonical_json(canonical) == record


def test_complete_record_matches_pinned_golden_bytes_and_digest() -> None:
    golden = GOLDEN.read_bytes().rstrip(b"\n")
    record = _record()
    assert record.canonical_json_bytes() == golden
    assert (
        record.digest()
        == "d15b9085eafc40958560a37a20f89271cccf734ea5004c12d91480a24e99fd3d"
    )


def test_decimal_negative_zero_is_canonical_zero() -> None:
    payload = json.loads(
        _record(technicals=_technicals(price=Decimal("-0.000"))).canonical_json()
    )
    assert payload["technicals"]["price"] == "0"


def test_reconstructed_policy_requires_all_enrichment_to_be_null() -> None:
    with pytest.raises(ValidationError, match="reconstructed enrichment"):
        _record(enrichment=EnrichmentV1(sector="Technology"))


def test_observed_bau_may_retain_observed_enrichment() -> None:
    record = _record(
        provenance_quality="observed_bau",
        enrichment=EnrichmentV1(sector="Technology", in_stocktwits=True),
        provenance=_provenance(
            point_in_time_universe=True,
            survivorship_bias="not_applicable",
            renamed_or_delisted_may_be_absent=False,
            historical_tradingview_screen_available=True,
        ),
    )
    assert record.enrichment.sector == "Technology"


def test_models_are_frozen_strict_forbid_extra_and_reject_non_finite() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.security_id = "different"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        TechnicalsV1.model_validate({**_technicals().model_dump(), "price": 1.2})
    with pytest.raises(ValidationError):
        TechnicalsV1.model_validate(
            {**_technicals().model_dump(), "price": Decimal("NaN")}
        )
    with pytest.raises(ValidationError):
        HistoricalScanRecordV1.model_validate({**record.model_dump(), "unknown": True})


def test_noncanonical_json_is_rejected_on_strict_round_trip() -> None:
    pretty = json.dumps(json.loads(_record().canonical_json()), indent=2).encode()
    with pytest.raises(HistoricalScanContractError) as caught:
        HistoricalScanRecordV1.from_canonical_json(pretty)
    assert caught.value.code == "integrity_error"


def test_fragment_envelope_is_closed_and_detector_discriminated() -> None:
    envelope = DetectorFragmentEnvelopeV1(
        schema_version="scan_detector_fragment.v1",
        security_id="sec-001",
        date=date(2026, 7, 31),
        detector="technical_indicators_v1",
        detector_version=DIGEST_A,
        detector_api_version="1",
        input_revision=DIGEST_B,
        result=TechnicalResultV1(technicals=_technicals()),
    )
    assert (
        DetectorFragmentEnvelopeV1.from_canonical_json(envelope.canonical_json_bytes())
        == envelope
    )

    with pytest.raises(ValidationError, match="result does not match detector"):
        DetectorFragmentEnvelopeV1.model_validate(
            {
                **envelope.model_dump(exclude={"result"}),
                "result": VcpResultV1(vcp=_vcp()),
            }
        )


def test_provenance_requires_exact_detector_version_set() -> None:
    with pytest.raises(ValidationError, match="detector versions"):
        _provenance(detector_versions={"technical_indicators_v1": DIGEST_A})


def test_nested_detector_versions_are_immutable() -> None:
    provenance = _provenance()
    with pytest.raises(TypeError, match="immutable"):
        provenance.detector_versions["vcp_v1"] = DIGEST_A


@pytest.mark.parametrize(
    "changes",
    [
        {"snapshot_month": "2026-06"},
        {"mic": "XLON", "currency": "USD", "quote_unit": "USD"},
        {"currency": "USD", "quote_unit": "GBp"},
    ],
)
def test_record_rejects_cross_field_and_technical_range_violations(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _record(**changes)


def test_technicals_reject_negative_and_inverted_ranges() -> None:
    with pytest.raises(ValidationError):
        _technicals(atr14=Decimal("-1"))
    with pytest.raises(ValidationError):
        _technicals(high_52w=Decimal("70"), low_52w=Decimal("80"))


def test_record_rejects_invalid_or_future_vcp_contractions() -> None:
    contraction = VcpContractionV1(
        label="T1",
        high_session=date(2026, 8, 3),
        high_price=Decimal("120"),
        low_session=date(2026, 8, 4),
        low_price=Decimal("110"),
        depth_pct=Decimal("8.33"),
        duration_sessions=1,
    )
    vcp = _vcp().model_copy(
        update={"num_contractions": 1, "contractions": (contraction,)}
    )
    with pytest.raises(ValidationError, match="as-of"):
        _record(vcp=vcp)

    with pytest.raises(ValidationError, match="high must not follow"):
        VcpContractionV1(
            label="T1",
            high_session=date(2026, 7, 2),
            high_price=Decimal("120"),
            low_session=date(2026, 7, 1),
            low_price=Decimal("110"),
            depth_pct=Decimal("8.33"),
            duration_sessions=1,
        )
