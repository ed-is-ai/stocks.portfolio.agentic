"""VCP runner over bounded split-continuous price evidence."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import importlib.util
import math
from pathlib import Path
from types import ModuleType
from typing import Mapping, cast

from app.services.backtest.detector_contracts import (
    DetectorContext,
    DetectorExecutionError,
)
from app.services.backtest.historical_scan_record import (
    DetectorResultV1,
    VcpContractionV1,
    VcpResultV1,
    VcpV1,
)


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
_CALCULATOR_ROOT = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "vcp-screener"
    / "scripts"
    / "calculators"
)


@lru_cache(maxsize=None)
def _calculator_module(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_historical_detector_{filename}", _CALCULATOR_ROOT / f"{filename}.py"
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


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} result is malformed")
    return value


def _keys(value: Mapping[str, object], name: str, *keys: str) -> None:
    if any(key not in value for key in keys):
        raise ValueError(f"{name} result is missing required fields")


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def run_vcp(context: DetectorContext) -> DetectorResultV1:
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
    # These are direct properties of the bounded sealed rows, not TechnicalsV1.
    quote = {
        "price": _finite_float(context.rows[-1].close),
        "yearHigh": _finite_float(max(row.high for row in context.rows)),
        "yearLow": _finite_float(min(row.low for row in context.rows)),
    }
    trend = _mapping(
        _calculator_module("trend_template_calculator").calculate_trend_template(
            newest_first,
            quote,
            rs_rank=None,
            max_sma200_extension=VCP_MAX_SMA200_EXTENSION,
        ),
        "trend template",
    )
    _keys(trend, "trend template", "score", "passed", "sma50", "sma200")
    pattern = _mapping(
        _calculator_module("vcp_pattern_calculator").calculate_vcp_pattern(
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
    _keys(
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
    if not isinstance(raw_contractions, list) or not all(
        isinstance(item, Mapping) for item in raw_contractions
    ):
        raise ValueError("VCP contractions are malformed")
    contractions_data = cast(list[Mapping[str, object]], raw_contractions)
    pivot_price = pattern["pivot_price"]
    last_low = contractions_data[-1]["low_price"] if contractions_data else None
    volume = _mapping(
        _calculator_module("volume_pattern_calculator").calculate_volume_pattern(
            newest_first,
            pivot_price=pivot_price,
            contractions=raw_contractions,
            breakout_volume_ratio=VCP_BREAKOUT_VOLUME_RATIO,
        ),
        "volume pattern",
    )
    if all(row.volume == 0 for row in context.rows):
        breakout, dry_up_ratio = False, None
    else:
        _keys(volume, "volume pattern", "breakout_volume_detected", "dry_up_ratio")
        breakout, dry_up_ratio = (
            _bool(volume["breakout_volume_detected"], "breakout volume"),
            volume["dry_up_ratio"],
        )
    pivot = _mapping(
        _calculator_module("pivot_proximity_calculator").calculate_pivot_proximity(
            current_price=quote["price"],
            pivot_price=pivot_price,
            last_contraction_low=last_low,
            breakout_volume=breakout,
        ),
        "pivot proximity",
    )
    _keys(pivot, "pivot proximity", "distance_from_pivot_pct")
    sma200 = _decimal(trend["sma200"], nullable=True)
    sma200_distance = (
        (quote["price"] - float(sma200)) / float(sma200) * 100 if sma200 else None
    )
    execution = _mapping(
        _calculator_module("execution_state").compute_execution_state(
            distance_from_pivot_pct=pivot["distance_from_pivot_pct"],
            price=quote["price"],
            sma50=trend["sma50"],
            sma200=trend["sma200"],
            sma200_distance_pct=sma200_distance,
            last_contraction_low=last_low,
            breakout_volume=breakout,
            max_sma200_extension=VCP_MAX_SMA200_EXTENSION,
        ),
        "execution state",
    )
    _keys(execution, "execution state", "state")
    bounded_sessions = {row.session for row in context.rows}
    contractions: list[VcpContractionV1] = []
    for index, item in enumerate(contractions_data, start=1):
        _keys(
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
        high_session, low_session = (
            date.fromisoformat(str(item["high_date"])),
            date.fromisoformat(str(item["low_date"])),
        )
        high_index, low_index = (
            _int(item["high_idx"], "contraction high index"),
            _int(item["low_idx"], "contraction low index"),
        )
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
                    "high_price": _decimal(item["high_price"]),
                    "low_session": low_session,
                    "low_price": _decimal(item["low_price"]),
                    "depth_pct": _decimal(item["depth_pct"]),
                    "duration_sessions": low_index - high_index,
                }
            )
        )
    return VcpResultV1(
        vcp=VcpV1.model_validate(
            {
                "valid_vcp": _bool(pattern["valid_vcp"], "valid VCP"),
                "score": _int(pattern["score"], "VCP score"),
                "trend_template_score": _decimal(trend["score"]),
                "trend_template_passed": _bool(
                    trend["passed"], "trend template passed"
                ),
                "wide_and_loose": _bool(pattern["wide_and_loose"], "wide and loose"),
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
    )
