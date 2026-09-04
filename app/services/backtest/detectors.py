"""Closed in-process historical detector registry and pure adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import importlib.util
import math
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping, Protocol, cast

from app.core.stage_classification import classify_weinstein_stage
from app.core.technical_indicators import (
    compute_reconstruction_technicals,
    weekly_closes,
)
from app.services.backtest.historical_scan_record import (
    DetectorFragmentEnvelopeV1,
    DetectorId,
    DetectorResultV1,
    FrozenDict,
    StageResultV1,
    StageV1,
    TechnicalResultV1,
    TechnicalsV1,
    VcpContractionV1,
    VcpResultV1,
    VcpV1,
)
from app.services.backtest.market_planes import SplitContinuousRow


DETECTOR_API_VERSION = "1"
REQUIRED_HISTORY_SESSIONS = 252
VCP_LOOKBACK_DAYS = 120
VCP_ATR_MULTIPLIER = 1.5
VCP_ATR_PERIOD = 14
VCP_MIN_CONTRACTION_DAYS = 5
VCP_MIN_CONTRACTIONS = 2
VCP_T1_DEPTH_MIN = 8.0
VCP_CONTRACTION_RATIO = 0.75
VCP_WIDE_AND_LOOSE_THRESHOLD = 15.0
VCP_BREAKOUT_VOLUME_RATIO = 1.5
VCP_MAX_SMA200_EXTENSION = 50.0

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CALCULATOR_ROOT = _PROJECT_ROOT / "skills" / "vcp-screener" / "scripts" / "calculators"


class DetectorExecutionError(ValueError):
    def __init__(self, code: str, detector: str, detail: str) -> None:
        self.code = code
        self.detector = detector
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class DetectorContext:
    rows: tuple[SplitContinuousRow, ...]
    technicals: TechnicalsV1 | None = None


class Detector(Protocol):
    detector_id: DetectorId
    detector_api_version: str
    required_history_sessions: int

    def run(self, context: DetectorContext) -> DetectorResultV1: ...


@dataclass(frozen=True)
class DetectorSpec:
    detector_id: DetectorId
    runner: Callable[[DetectorContext], DetectorResultV1]
    configuration: Mapping[str, object]
    detector_api_version: str = DETECTOR_API_VERSION
    required_history_sessions: int = REQUIRED_HISTORY_SESSIONS

    def run(self, context: DetectorContext) -> DetectorResultV1:
        _validate_context(self.detector_id, context)
        try:
            return self.runner(context)
        except DetectorExecutionError:
            raise
        except Exception as exc:
            raise DetectorExecutionError(
                "integrity_error", self.detector_id, "detector output is invalid"
            ) from exc


@dataclass(frozen=True)
class DetectorSuiteResult:
    """The complete ordered output of the canonical detector registry."""

    technicals: TechnicalsV1
    stage: StageV1
    vcp: VcpV1
    fragments: tuple[DetectorFragmentEnvelopeV1, ...]


def _validate_context(detector: DetectorId, context: DetectorContext) -> None:
    if len(context.rows) != REQUIRED_HISTORY_SESSIONS:
        raise DetectorExecutionError(
            "required_data_missing", detector, "252 completed sessions are required"
        )
    sessions = tuple(row.session for row in context.rows)
    if sessions != tuple(sorted(sessions)) or len(set(sessions)) != len(sessions):
        raise DetectorExecutionError(
            "integrity_error", detector, "detector sessions are invalid"
        )
    for row in context.rows:
        if row.volume is None:
            raise DetectorExecutionError(
                "required_data_missing", detector, "detector volume is missing"
            )
        values = (row.open, row.high, row.low, row.close, row.volume)
        if not all(value.is_finite() for value in values):
            raise DetectorExecutionError(
                "integrity_error", detector, "detector input is not finite"
            )


def _technical_runner(context: DetectorContext) -> DetectorResultV1:
    return TechnicalResultV1(technicals=compute_reconstruction_technicals(context.rows))


def _require_technicals(detector: DetectorId, context: DetectorContext) -> TechnicalsV1:
    if context.technicals is None:
        raise DetectorExecutionError(
            "integrity_error", detector, "technical detector output is required"
        )
    return context.technicals


def _stage_runner(context: DetectorContext) -> DetectorResultV1:
    technicals = _require_technicals("weinstein_stage_v1", context)
    stage = classify_weinstein_stage(
        price=technicals.price,
        sma150=technicals.sma150,
        sma200=technicals.sma200,
        price_history=weekly_closes(context.rows),
    )
    return StageResultV1(stage=StageV1.model_validate({"value": stage}))


@lru_cache(maxsize=None)
def _calculator_module(filename: str) -> ModuleType:
    path = _CALCULATOR_ROOT / f"{filename}.py"
    spec = importlib.util.spec_from_file_location(
        f"_historical_detector_{filename}", path
    )
    if spec is None or spec.loader is None:
        raise DetectorExecutionError(
            "integrity_error", "vcp_v1", "calculator source is unavailable"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _finite_float(value: Decimal) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("detector value is outside finite float range")
    return result


def _decimal(value: object, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a detector decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("detector decimal is invalid") from exc
    if not result.is_finite():
        raise ValueError("detector decimal is not finite")
    return result


def _result_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} result is malformed")
    return value


def _require_keys(value: Mapping[str, object], name: str, *keys: str) -> None:
    if any(key not in value for key in keys):
        raise ValueError(f"{name} result is missing required fields")


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _vcp_runner(context: DetectorContext) -> DetectorResultV1:
    technicals = _require_technicals("vcp_v1", context)
    newest_first = [
        {
            "date": row.session.isoformat(),
            "open": _finite_float(row.open),
            "high": _finite_float(row.high),
            "low": _finite_float(row.low),
            "close": _finite_float(row.close),
            "volume": cast(Decimal, row.volume),
        }
        for row in reversed(context.rows)
    ]
    quote = {
        "price": _finite_float(technicals.price),
        "yearHigh": _finite_float(technicals.high_52w),
        "yearLow": _finite_float(technicals.low_52w),
    }

    trend_module = _calculator_module("trend_template_calculator")
    pattern_module = _calculator_module("vcp_pattern_calculator")
    volume_module = _calculator_module("volume_pattern_calculator")
    pivot_module = _calculator_module("pivot_proximity_calculator")
    execution_module = _calculator_module("execution_state")

    trend = _result_mapping(
        trend_module.calculate_trend_template(
            newest_first,
            quote,
            rs_rank=None,
            max_sma200_extension=VCP_MAX_SMA200_EXTENSION,
        ),
        "trend template",
    )
    _require_keys(trend, "trend template", "score", "passed", "sma50", "sma200")
    pattern = _result_mapping(
        pattern_module.calculate_vcp_pattern(
            newest_first,
            lookback_days=VCP_LOOKBACK_DAYS,
            atr_multiplier=VCP_ATR_MULTIPLIER,
            atr_period=VCP_ATR_PERIOD,
            min_contraction_days=VCP_MIN_CONTRACTION_DAYS,
            min_contractions=VCP_MIN_CONTRACTIONS,
            t1_depth_min=VCP_T1_DEPTH_MIN,
            contraction_ratio=VCP_CONTRACTION_RATIO,
            wide_and_loose_threshold=VCP_WIDE_AND_LOOSE_THRESHOLD,
        ),
        "VCP pattern",
    )
    _require_keys(
        pattern,
        "VCP pattern",
        "valid_vcp",
        "score",
        "contractions",
        "pivot_price",
        "atr_compression_ratio",
        "wide_and_loose",
        "right_side_range_ratio",
    )
    raw_contractions = pattern["contractions"]
    if not isinstance(raw_contractions, list):
        raise ValueError("VCP contractions are malformed")
    if not all(isinstance(item, Mapping) for item in raw_contractions):
        raise ValueError("VCP contraction is malformed")
    typed_contractions = cast(list[Mapping[str, object]], raw_contractions)
    pivot_price = pattern["pivot_price"]
    last_low = typed_contractions[-1]["low_price"] if typed_contractions else None
    volume = _result_mapping(
        volume_module.calculate_volume_pattern(
            newest_first,
            pivot_price=pivot_price,
            contractions=raw_contractions,
            breakout_volume_ratio=VCP_BREAKOUT_VOLUME_RATIO,
        ),
        "volume pattern",
    )
    if all(row.volume == 0 for row in context.rows):
        breakout = False
        dry_up_ratio: object = None
    else:
        _require_keys(
            volume,
            "volume pattern",
            "breakout_volume_detected",
            "dry_up_ratio",
        )
        breakout = _strict_bool(volume["breakout_volume_detected"], "breakout volume")
        dry_up_ratio = volume["dry_up_ratio"]
    pivot = _result_mapping(
        pivot_module.calculate_pivot_proximity(
            current_price=quote["price"],
            pivot_price=pivot_price,
            last_contraction_low=last_low,
            breakout_volume=breakout,
        ),
        "pivot proximity",
    )
    _require_keys(pivot, "pivot proximity", "distance_from_pivot_pct")
    sma200_value = trend["sma200"]
    sma200_decimal = _decimal(sma200_value, nullable=True)
    sma200_distance = (
        (quote["price"] - float(sma200_decimal)) / float(sma200_decimal) * 100
        if sma200_decimal
        else None
    )
    execution = _result_mapping(
        execution_module.compute_execution_state(
            distance_from_pivot_pct=pivot["distance_from_pivot_pct"],
            price=quote["price"],
            sma50=trend["sma50"],
            sma200=sma200_value,
            sma200_distance_pct=sma200_distance,
            last_contraction_low=last_low,
            breakout_volume=breakout,
            max_sma200_extension=VCP_MAX_SMA200_EXTENSION,
        ),
        "execution state",
    )
    _require_keys(execution, "execution state", "state")

    contractions: list[VcpContractionV1] = []
    bounded_sessions = {row.session for row in context.rows}
    for index, item in enumerate(typed_contractions, start=1):
        _require_keys(
            item,
            "VCP contraction",
            "high_date",
            "high_price",
            "low_date",
            "low_price",
            "depth_pct",
            "high_idx",
            "low_idx",
        )
        high_session = date.fromisoformat(str(item["high_date"]))
        low_session = date.fromisoformat(str(item["low_date"]))
        high_index = _strict_int(item["high_idx"], "contraction high index")
        low_index = _strict_int(item["low_idx"], "contraction low index")
        if (
            max(high_session, low_session) > context.rows[-1].session
            or high_session not in bounded_sessions
            or low_session not in bounded_sessions
            or not 0 <= high_index <= low_index < VCP_LOOKBACK_DAYS
        ):
            raise ValueError("VCP contraction exceeds bounded evidence")
        contractions.append(
            VcpContractionV1.model_validate(
                {
                    "label": f"T{index}",
                    "high_session": high_session,
                    "high_price": cast(Decimal, _decimal(item["high_price"])),
                    "low_session": low_session,
                    "low_price": cast(Decimal, _decimal(item["low_price"])),
                    "depth_pct": cast(Decimal, _decimal(item["depth_pct"])),
                    "duration_sessions": low_index - high_index,
                }
            )
        )

    model = VcpV1.model_validate(
        {
            "valid_vcp": _strict_bool(pattern["valid_vcp"], "valid VCP"),
            "score": _strict_int(pattern["score"], "VCP score"),
            "trend_template_score": cast(Decimal, _decimal(trend["score"])),
            "trend_template_passed": _strict_bool(
                trend["passed"], "trend template passed"
            ),
            "wide_and_loose": _strict_bool(pattern["wide_and_loose"], "wide and loose"),
            "breakout_volume_detected": breakout,
            "num_contractions": len(contractions),
            "contractions": tuple(contractions),
            "pivot_price": _decimal(pivot_price, nullable=True),
            "last_contraction_low": _decimal(last_low, nullable=True),
            "atr_compression_ratio": _decimal(
                pattern["atr_compression_ratio"], nullable=True
            ),
            "right_side_range_ratio": _decimal(
                pattern["right_side_range_ratio"], nullable=True
            ),
            "dry_up_ratio": _decimal(dry_up_ratio, nullable=True),
            "distance_from_pivot_pct": _decimal(
                pivot["distance_from_pivot_pct"], nullable=True
            ),
            "execution_state": execution["state"],
        }
    )
    return VcpResultV1(vcp=model)


DETECTOR_REGISTRY: tuple[DetectorSpec, ...] = (
    DetectorSpec(
        "technical_indicators_v1",
        _technical_runner,
        FrozenDict({"required_history_sessions": REQUIRED_HISTORY_SESSIONS}),
    ),
    DetectorSpec(
        "weinstein_stage_v1",
        _stage_runner,
        FrozenDict(
            {
                "required_history_sessions": REQUIRED_HISTORY_SESSIONS,
                "sma150_slope_window_weeks": 30,
                "sma200_slope_window_weeks": 40,
                "slope_lookback_weeks": 4,
            }
        ),
    ),
    DetectorSpec(
        "vcp_v1",
        _vcp_runner,
        FrozenDict(
            {
                "required_history_sessions": REQUIRED_HISTORY_SESSIONS,
                "rs_rank": None,
                "lookback_days": VCP_LOOKBACK_DAYS,
                "atr_multiplier": VCP_ATR_MULTIPLIER,
                "atr_period": VCP_ATR_PERIOD,
                "min_contraction_days": VCP_MIN_CONTRACTION_DAYS,
                "min_contractions": VCP_MIN_CONTRACTIONS,
                "t1_depth_min": VCP_T1_DEPTH_MIN,
                "contraction_ratio": VCP_CONTRACTION_RATIO,
                "wide_and_loose_threshold": VCP_WIDE_AND_LOOSE_THRESHOLD,
                "breakout_volume_ratio": VCP_BREAKOUT_VOLUME_RATIO,
                "max_sma200_extension": VCP_MAX_SMA200_EXTENSION,
            }
        ),
    ),
)


def required_history_sessions() -> int:
    return max(detector.required_history_sessions for detector in DETECTOR_REGISTRY)


def run_detector_suite(
    rows: tuple[SplitContinuousRow, ...],
    *,
    security_id: str,
    as_of_session: date,
    detector_versions: Mapping[str, str],
    input_revision: str,
) -> DetectorSuiteResult:
    """Run the registry once and return an all-or-error typed detector suite."""
    technicals: TechnicalsV1 | None = None
    stage: StageV1 | None = None
    vcp: VcpV1 | None = None
    fragments: list[DetectorFragmentEnvelopeV1] = []
    for detector in DETECTOR_REGISTRY:
        result = detector.run(DetectorContext(rows, technicals))
        fragments.append(
            DetectorFragmentEnvelopeV1(
                schema_version="scan_detector_fragment.v1",
                security_id=security_id,
                date=as_of_session,
                detector=detector.detector_id,
                detector_version=detector_versions[detector.detector_id],
                detector_api_version=detector.detector_api_version,
                input_revision=input_revision,
                result=result,
            )
        )
        if isinstance(result, TechnicalResultV1):
            technicals = result.technicals
        elif isinstance(result, StageResultV1):
            stage = result.stage
        elif isinstance(result, VcpResultV1):
            vcp = result.vcp
    if technicals is None or stage is None or vcp is None:
        raise DetectorExecutionError(
            "integrity_error", "registry", "detector suite output is incomplete"
        )
    return DetectorSuiteResult(technicals, stage, vcp, tuple(fragments))


def detector_by_id(detector_id: str) -> DetectorSpec:
    for detector in DETECTOR_REGISTRY:
        if detector.detector_id == detector_id:
            return detector
    raise DetectorExecutionError(
        "provider_contract_error", detector_id, "detector is not supported"
    )


__all__ = [
    "DETECTOR_API_VERSION",
    "DETECTOR_REGISTRY",
    "Detector",
    "DetectorContext",
    "DetectorExecutionError",
    "DetectorSpec",
    "detector_by_id",
    "required_history_sessions",
]
