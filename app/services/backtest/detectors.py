"""Closed in-process historical detector registry and pure adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping, Protocol

from app.services.backtest.detector_contracts import (
    DetectorContext,
    DetectorExecutionError,
    REQUIRED_HISTORY_SESSIONS,
)
from app.services.backtest.stage_detector import run_stage
from app.services.backtest.technical_detector import run_technicals
from app.services.backtest.vcp_detector import (
    VCP_ATR_MULTIPLIER,
    VCP_ATR_PERIOD,
    VCP_BREAKOUT_VOLUME_RATIO,
    VCP_CONTRACTION_RATIO,
    VCP_LOOKBACK_DAYS,
    VCP_MAX_SMA200_EXTENSION,
    VCP_MIN_CONTRACTIONS,
    VCP_MIN_CONTRACTION_DAYS,
    VCP_T1_DEPTH_MIN,
    VCP_WIDE_AND_LOOSE_THRESHOLD,
    run_vcp,
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
    VcpResultV1,
    VcpV1,
)
from app.services.backtest.market_planes import SplitContinuousRow


DETECTOR_API_VERSION = "1"


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
        context.validate(self.detector_id)
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


DETECTOR_REGISTRY: tuple[DetectorSpec, ...] = (
    DetectorSpec(
        "technical_indicators_v1",
        run_technicals,
        FrozenDict({"required_history_sessions": REQUIRED_HISTORY_SESSIONS}),
    ),
    DetectorSpec(
        "weinstein_stage_v1",
        run_stage,
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
        run_vcp,
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
