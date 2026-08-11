"""Closed, canonical historical scanner record and detector-fragment contracts."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import unicodedata
from typing import Annotated, Any, ClassVar, Literal, NoReturn, TypeVar, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)


DetectorId = Literal["technical_indicators_v1", "weinstein_stage_v1", "vcp_v1"]
DETECTOR_IDS: tuple[str, ...] = (
    "technical_indicators_v1",
    "weinstein_stage_v1",
    "vcp_v1",
)
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]


class FrozenDict(dict[str, Any]):
    """JSON-compatible mapping that cannot be mutated after validation."""

    @staticmethod
    def _raise_immutable() -> NoReturn:
        raise TypeError("mapping is immutable")

    def __setitem__(self, key: str, value: Any) -> None:
        self._raise_immutable()

    def __delitem__(self, key: str) -> None:
        self._raise_immutable()

    def clear(self) -> None:
        self._raise_immutable()

    def pop(self, key: str, default: Any = None) -> Any:
        self._raise_immutable()

    def popitem(self) -> tuple[str, Any]:
        self._raise_immutable()

    def setdefault(self, key: str, default: Any = None) -> Any:
        self._raise_immutable()

    def update(self, other: object = (), /, **kwargs: Any) -> None:
        self._raise_immutable()

    def __ior__(self, value: object) -> "FrozenDict":
        self._raise_immutable()


def frozen_dict(value: dict[str, Any]) -> FrozenDict:
    return FrozenDict(dict(value))


class HistoricalScanContractError(ValueError):
    """Stable failure for malformed or non-canonical historical scan payloads."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validated_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("decimal must be finite")
    if value == 0:
        return Decimal(0)
    return value.normalize()


def canonical_decimal(value: Decimal) -> str:
    value = _validated_decimal(value)
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


CanonicalDecimal = Annotated[
    Decimal,
    AfterValidator(_validated_decimal),
    PlainSerializer(canonical_decimal, return_type=str, when_used="json"),
]


def _normalize_strings(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {
            _normalize_strings(key): _normalize_strings(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_normalize_strings(item) for item in value)
    if isinstance(value, list):
        return [_normalize_strings(item) for item in value]
    return value


ModelT = TypeVar("ModelT", bound="CanonicalModel")


class CanonicalModel(BaseModel):
    """Strict immutable model with one canonical UTF-8 JSON representation."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_unicode(cls, value: Any) -> Any:
        return _normalize_strings(value)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def canonical_json_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    def digest(self) -> str:
        return sha256(self.canonical_json_bytes()).hexdigest()

    @classmethod
    def from_canonical_json(cls: type[ModelT], payload: str | bytes) -> ModelT:
        raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        try:
            # Wire types (ISO dates, decimal strings, JSON arrays) necessarily
            # decode before the strict Python model can be reconstructed. The
            # byte-for-byte canonical comparison below rejects every lossy or
            # alternative coercion.
            parsed = cls.model_validate_json(raw, strict=False)
        except Exception as exc:
            raise HistoricalScanContractError(
                "integrity_error", "historical scan JSON is invalid"
            ) from exc
        if parsed.canonical_json_bytes() != raw:
            raise HistoricalScanContractError(
                "integrity_error", "historical scan JSON is not canonical"
            )
        return parsed


class TechnicalsV1(CanonicalModel):
    price: CanonicalDecimal
    sma10: CanonicalDecimal
    sma30: CanonicalDecimal
    sma50: CanonicalDecimal
    sma150: CanonicalDecimal
    sma200: CanonicalDecimal
    rsi14: Annotated[CanonicalDecimal, Field(ge=Decimal("0"), le=Decimal("100"))]
    atr14: CanonicalDecimal
    volume: CanonicalDecimal
    vol_ma50: CanonicalDecimal
    rel_volume: CanonicalDecimal
    high_52w: CanonicalDecimal
    low_52w: CanonicalDecimal
    high_base: CanonicalDecimal
    handle_low: CanonicalDecimal
    pct_from_52w_high: CanonicalDecimal
    pct_change_week: CanonicalDecimal

    @model_validator(mode="after")
    def _valid_ranges(self) -> "TechnicalsV1":
        positive = (
            self.price,
            self.sma10,
            self.sma30,
            self.sma50,
            self.sma150,
            self.sma200,
            self.high_52w,
            self.low_52w,
            self.high_base,
            self.handle_low,
        )
        if any(value < 0 for value in positive):
            raise ValueError("technical prices and moving averages cannot be negative")
        non_negative = (
            self.atr14,
            self.volume,
            self.vol_ma50,
            self.rel_volume,
        )
        if any(value < 0 for value in non_negative):
            raise ValueError("technical ranges cannot be negative")
        if self.high_52w < self.low_52w or self.high_base < self.handle_low:
            raise ValueError("technical high/low ranges are invalid")
        return self


class StageV1(CanonicalModel):
    value: Literal["Stage 1", "Stage 2", "Stage 3", "Stage 4"]


class VcpContractionV1(CanonicalModel):
    label: Literal["T1", "T2", "T3", "T4"]
    high_session: date
    high_price: CanonicalDecimal
    low_session: date
    low_price: CanonicalDecimal
    depth_pct: CanonicalDecimal
    duration_sessions: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _valid_contraction(self) -> "VcpContractionV1":
        if self.high_session > self.low_session:
            raise ValueError("contraction high must not follow its low")
        if self.high_price <= 0 or self.low_price <= 0 or self.depth_pct < 0:
            raise ValueError("contraction prices and depth are invalid")
        if self.high_session == self.low_session and self.duration_sessions != 0:
            raise ValueError("same-session contraction must have zero duration")
        return self


class VcpV1(CanonicalModel):
    valid_vcp: bool
    score: Annotated[int, Field(ge=0, le=100)]
    trend_template_score: Annotated[
        CanonicalDecimal, Field(ge=Decimal("0"), le=Decimal("100"))
    ]
    trend_template_passed: bool
    wide_and_loose: bool
    breakout_volume_detected: bool
    num_contractions: Annotated[int, Field(ge=0, le=4)]
    contractions: tuple[VcpContractionV1, ...]
    pivot_price: CanonicalDecimal | None
    last_contraction_low: CanonicalDecimal | None
    atr_compression_ratio: CanonicalDecimal | None
    right_side_range_ratio: CanonicalDecimal | None
    dry_up_ratio: CanonicalDecimal | None
    distance_from_pivot_pct: CanonicalDecimal | None
    execution_state: Literal[
        "Invalid",
        "Damaged",
        "Overextended",
        "Extended",
        "Early-post-breakout",
        "Breakout",
        "Pre-breakout",
    ]

    @model_validator(mode="after")
    def _contraction_count_matches(self) -> "VcpV1":
        if self.num_contractions != len(self.contractions):
            raise ValueError("num_contractions must equal contractions length")
        expected = tuple(f"T{index}" for index in range(1, len(self.contractions) + 1))
        if tuple(item.label for item in self.contractions) != expected:
            raise ValueError("contractions must be ordered and consecutively labelled")
        if any(
            left.low_session > right.high_session
            for left, right in zip(self.contractions, self.contractions[1:])
        ):
            raise ValueError("contractions must be chronologically ordered")
        positive_nullable = (
            self.pivot_price,
            self.last_contraction_low,
        )
        if any(value is not None and value <= 0 for value in positive_nullable):
            raise ValueError("VCP prices must be positive when present")
        ratio_nullable = (
            self.atr_compression_ratio,
            self.right_side_range_ratio,
            self.dry_up_ratio,
        )
        if any(value is not None and value < 0 for value in ratio_nullable):
            raise ValueError("VCP ratios cannot be negative")
        return self


class EnrichmentV1(CanonicalModel):
    sector: str | None = None
    observed_source: str | None = None
    eps_growth: CanonicalDecimal | None = None
    annual_eps_growth: CanonicalDecimal | None = None
    roe: CanonicalDecimal | None = None
    inst_ownership_pct: CanonicalDecimal | None = None
    pe_ratio: CanonicalDecimal | None = None
    rel_strength_vs_spy: CanonicalDecimal | None = None
    inst_count: int | None = None
    funds_buying: int | None = None
    funds_selling: int | None = None
    funds_net: int | None = None
    congress_buys: int | None = None
    congress_sells: int | None = None
    senate_buys: int | None = None
    senate_sells: int | None = None
    spy_uptrend: bool | None = None
    in_stocktwits: bool | None = None
    in_whale_wisdom: bool | None = None


class ProvenanceV1(CanonicalModel):
    price_provider: Literal["yfinance"]
    universe_basis: Literal["captured_configured_roster"]
    roster_captured_at: datetime
    point_in_time_universe: bool
    survivorship_bias: Literal["known", "not_applicable"]
    renamed_or_delisted_may_be_absent: bool
    historical_tradingview_screen_available: bool
    roster_digest: Digest
    alias_revision: Digest
    calendar_dataset_version: NonEmpty
    calendar_dataset_digest: Digest
    provider_evidence_manifest_digest: Digest
    provider_data_revision: Digest
    provider_request_contract_version: NonEmpty
    yfinance_ingestion_version: NonEmpty
    input_revision: Digest
    detector_versions: dict[str, Digest]

    @field_validator("roster_captured_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("roster_captured_at must be a UTC instant")
        return value.astimezone(timezone.utc)

    @field_validator("detector_versions")
    @classmethod
    def _exact_detector_set(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(DETECTOR_IDS):
            raise ValueError("detector versions must contain the exact V1 detector set")
        return cast(dict[str, str], frozen_dict(dict(sorted(value.items()))))


class ReconstructabilityPolicyV1(CanonicalModel):
    version: Literal["reconstructability.v1"] = "reconstructability.v1"
    reconstructed_nullable_fields: ClassVar[frozenset[str]] = frozenset(
        f"enrichment.{name}" for name in EnrichmentV1.model_fields
    )

    @classmethod
    def validate_record(cls, record: "HistoricalScanRecordV1") -> None:
        if record.provenance_quality != "best_effort_reconstructed":
            return
        if any(value is not None for value in record.enrichment.model_dump().values()):
            raise ValueError("reconstructed enrichment fields must all be null")
        provenance = record.provenance
        if (
            provenance.point_in_time_universe
            or provenance.survivorship_bias != "known"
            or not provenance.renamed_or_delisted_may_be_absent
            or provenance.historical_tradingview_screen_available
        ):
            raise ValueError("reconstructed provenance facts violate policy")


class HistoricalScanRecordV1(CanonicalModel):
    schema_version: Literal["historical_scan_record.v1"]
    security_id: NonEmpty
    observed_symbol: NonEmpty
    mic: Literal["XNAS", "XNYS", "XLON"]
    snapshot_month: Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
    as_of_session_date: date
    currency: Literal["USD", "GBP"]
    quote_unit: Literal["USD", "GBP", "GBp"]
    provenance_quality: Literal["best_effort_reconstructed", "observed_bau"]
    technicals: TechnicalsV1
    stage: StageV1
    vcp: VcpV1
    enrichment: EnrichmentV1
    provenance: ProvenanceV1

    @model_validator(mode="after")
    def _apply_reconstructability_policy(self) -> "HistoricalScanRecordV1":
        if self.snapshot_month != self.as_of_session_date.strftime("%Y-%m"):
            raise ValueError("snapshot month must contain the as-of session")
        expected_currency = "USD" if self.mic in {"XNAS", "XNYS"} else "GBP"
        expected_units = {"USD"} if expected_currency == "USD" else {"GBP", "GBp"}
        if self.currency != expected_currency or self.quote_unit not in expected_units:
            raise ValueError("MIC, currency, and quote unit are inconsistent")
        if any(
            max(item.high_session, item.low_session) > self.as_of_session_date
            for item in self.vcp.contractions
        ):
            raise ValueError("VCP contraction exceeds the record as-of session")
        ReconstructabilityPolicyV1.validate_record(self)
        return self


class TechnicalResultV1(CanonicalModel):
    technicals: TechnicalsV1


class StageResultV1(CanonicalModel):
    stage: StageV1


class VcpResultV1(CanonicalModel):
    vcp: VcpV1


DetectorResultV1 = TechnicalResultV1 | StageResultV1 | VcpResultV1


class DetectorFragmentEnvelopeV1(CanonicalModel):
    schema_version: Literal["scan_detector_fragment.v1"]
    security_id: NonEmpty
    date: date
    detector: DetectorId
    detector_version: Digest
    detector_api_version: NonEmpty
    input_revision: Digest
    result: DetectorResultV1

    @model_validator(mode="after")
    def _result_matches_detector(self) -> "DetectorFragmentEnvelopeV1":
        expected: dict[str, type[CanonicalModel]] = {
            "technical_indicators_v1": TechnicalResultV1,
            "weinstein_stage_v1": StageResultV1,
            "vcp_v1": VcpResultV1,
        }
        if not isinstance(self.result, expected[self.detector]):
            raise ValueError("result does not match detector")
        return self


__all__ = [
    "CanonicalDecimal",
    "DetectorFragmentEnvelopeV1",
    "DetectorId",
    "EnrichmentV1",
    "FrozenDict",
    "HistoricalScanContractError",
    "HistoricalScanRecordV1",
    "ProvenanceV1",
    "ReconstructabilityPolicyV1",
    "StageResultV1",
    "StageV1",
    "TechnicalResultV1",
    "TechnicalsV1",
    "VcpContractionV1",
    "VcpResultV1",
    "VcpV1",
    "canonical_decimal",
    "frozen_dict",
]
