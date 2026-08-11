from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, getcontext
import math
from types import SimpleNamespace

import pandas as pd
import pytest

import app.services.backtest.detectors as detector_module
from app.core.stage_classification import classify_weinstein_stage, sma_slope
from app.core.technical_indicators import compute_live_technicals
from app.services.backtest.detectors import (
    DETECTOR_REGISTRY,
    DetectorContext,
    DetectorExecutionError,
    required_history_sessions,
)
from app.services.backtest.historical_scan_record import (
    StageResultV1,
    TechnicalResultV1,
    VcpResultV1,
)
from app.services.backtest.market_planes import SplitContinuousRow


def test_live_technical_core_preserves_scanner_characterization(
    sample_stock_data: pd.DataFrame,
) -> None:
    result = compute_live_technicals(sample_stock_data.copy())

    assert result == {
        "price": 104.0,
        "price_history": [100.0, 101.0, 102.0, 103.0, 104.0],
        "sma10": None,
        "sma30": None,
        "sma50": None,
        "sma150": None,
        "sma200": None,
        "rsi14": None,
        "atr14": None,
        "volume": 1_400_000,
        "vol_ma50": None,
        "rel_volume": 1.0,
        "high_52w": 109.0,
        "low_52w": 95.0,
        "high_base": 109.0,
        "handle_low": 95.0,
        "pct_from_52w_high": -4.6,
        "pct_change_week": 4.0,
        "ohlcv_history": [
            {
                "date": "2023-01-29",
                "open": 103.0,
                "high": 109.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 1_400_000,
            },
            {
                "date": "2023-01-22",
                "open": 102.0,
                "high": 108.0,
                "low": 98.0,
                "close": 103.0,
                "volume": 1_300_000,
            },
            {
                "date": "2023-01-15",
                "open": 101.0,
                "high": 107.0,
                "low": 97.0,
                "close": 102.0,
                "volume": 1_200_000,
            },
            {
                "date": "2023-01-08",
                "open": 100.0,
                "high": 106.0,
                "low": 96.0,
                "close": 101.0,
                "volume": 1_100_000,
            },
            {
                "date": "2023-01-01",
                "open": 99.0,
                "high": 105.0,
                "low": 95.0,
                "close": 100.0,
                "volume": 1_000_000,
            },
        ],
    }


def test_stage_core_preserves_weinstein_rules() -> None:
    rising = [float(index) for index in range(1, 53)]
    falling = list(reversed(rising))

    assert sma_slope(rising, window=40, lookback=4) is not None
    assert (
        classify_weinstein_stage(
            price=100.0, sma150=75.0, sma200=70.0, price_history=rising
        )
        == "Stage 2"
    )
    assert (
        classify_weinstein_stage(
            price=50.0, sma150=80.0, sma200=85.0, price_history=falling
        )
        == "Stage 4"
    )
    assert (
        classify_weinstein_stage(
            price=75.0, sma150=80.0, sma200=70.0, price_history=rising
        )
        == "Stage 3"
    )


def test_detector_registry_is_fixed_order_and_inclusive_252_sessions() -> None:
    assert tuple(detector.detector_id for detector in DETECTOR_REGISTRY) == (
        "technical_indicators_v1",
        "weinstein_stage_v1",
        "vcp_v1",
    )
    assert all(
        detector.required_history_sessions == 252 for detector in DETECTOR_REGISTRY
    )
    assert required_history_sessions() == 252
    assert all(detector.detector_api_version == "1" for detector in DETECTOR_REGISTRY)


def test_stage_core_accepts_decimal_inputs_without_agent_dependency() -> None:
    assert (
        classify_weinstein_stage(
            price=Decimal("100"),
            sma150=Decimal("75"),
            sma200=Decimal("70"),
            price_history=[Decimal(index) for index in range(1, 53)],
        )
        == "Stage 2"
    )


def _historical_rows() -> tuple[SplitContinuousRow, ...]:
    sessions: list[date] = []
    current = date(2025, 8, 1)
    while len(sessions) < 252:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    rows: list[SplitContinuousRow] = []
    for index, session in enumerate(sessions):
        close = Decimal(str(80 + index * 0.12 + math.sin(index / 7) * 2))
        rows.append(
            SplitContinuousRow(
                evidence_revision="evidence-revision",
                session=session,
                open=close - Decimal("0.5"),
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("1000.125") + Decimal(index % 11),
            )
        )
    return tuple(rows)


def test_registered_detectors_run_in_process_over_fractional_bounded_rows() -> None:
    rows = _historical_rows()
    technical_result = DETECTOR_REGISTRY[0].run(DetectorContext(rows))
    assert isinstance(technical_result, TechnicalResultV1)
    assert technical_result.technicals.volume == Decimal("1000.125") + Decimal(9)

    context = DetectorContext(rows, technical_result.technicals)
    stage_result = DETECTOR_REGISTRY[1].run(context)
    vcp_result = DETECTOR_REGISTRY[2].run(context)

    assert isinstance(stage_result, StageResultV1)
    assert stage_result.stage.value in {"Stage 1", "Stage 2", "Stage 3", "Stage 4"}
    assert isinstance(vcp_result, VcpResultV1)
    assert isinstance(vcp_result.vcp.valid_vcp, bool)
    assert vcp_result.vcp.num_contractions == len(vcp_result.vcp.contractions)
    assert vcp_result.vcp.execution_state in {
        "Invalid",
        "Damaged",
        "Overextended",
        "Extended",
        "Early-post-breakout",
        "Breakout",
        "Pre-breakout",
    }


def test_well_formed_no_pattern_is_a_valid_false_vcp_result() -> None:
    flat_rows = tuple(
        replace(
            row,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000.125"),
        )
        for row in _historical_rows()
    )
    technical = DETECTOR_REGISTRY[0].run(DetectorContext(flat_rows))
    assert isinstance(technical, TechnicalResultV1)
    vcp = DETECTOR_REGISTRY[2].run(DetectorContext(flat_rows, technical.technicals))
    assert isinstance(vcp, VcpResultV1)
    assert vcp.vcp.valid_vcp is False


def test_vcp_adapter_rejects_coerced_or_missing_calculator_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _historical_rows()
    technical = DETECTOR_REGISTRY[0].run(DetectorContext(rows))
    assert isinstance(technical, TechnicalResultV1)
    real_loader = detector_module._calculator_module

    malformed_pattern = SimpleNamespace(
        calculate_vcp_pattern=lambda *_args, **_kwargs: {
            "valid_vcp": "false",
            "score": 0,
            "contractions": [],
            "pivot_price": None,
            "atr_compression_ratio": None,
            "wide_and_loose": False,
            "right_side_range_ratio": None,
        }
    )

    def loader(name: str):
        return (
            malformed_pattern if name == "vcp_pattern_calculator" else real_loader(name)
        )

    monkeypatch.setattr(detector_module, "_calculator_module", loader)
    with pytest.raises(DetectorExecutionError) as caught:
        DETECTOR_REGISTRY[2].run(DetectorContext(rows, technical.technicals))
    assert caught.value.code == "integrity_error"


def test_unexpected_detector_exception_is_mapped_to_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _historical_rows()
    technical = DETECTOR_REGISTRY[0].run(DetectorContext(rows))
    assert isinstance(technical, TechnicalResultV1)
    real_loader = detector_module._calculator_module

    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("volatile detail")

    broken_pattern = SimpleNamespace(calculate_vcp_pattern=explode)

    def loader(name: str):
        return broken_pattern if name == "vcp_pattern_calculator" else real_loader(name)

    monkeypatch.setattr(detector_module, "_calculator_module", loader)
    with pytest.raises(DetectorExecutionError) as caught:
        DETECTOR_REGISTRY[2].run(DetectorContext(rows, technical.technicals))
    assert caught.value.code == "integrity_error"
    assert caught.value.detail == "detector output is invalid"


def test_volume_calculator_is_context_independent_and_preserves_live_integers() -> None:
    calculator = detector_module._calculator_module("volume_pattern_calculator")
    integral_rows = [{"close": 100, "volume": 1000 + index % 2} for index in range(50)]
    original_precision = getcontext().prec
    try:
        getcontext().prec = 6
        low_precision = calculator.calculate_volume_pattern(integral_rows)
        getcontext().prec = 50
        high_precision = calculator.calculate_volume_pattern(integral_rows)
    finally:
        getcontext().prec = original_precision

    assert low_precision == high_precision
    assert low_precision["avg_volume_50d"] == 1000
    assert type(low_precision["avg_volume_50d"]) is int

    fractional = calculator.calculate_volume_pattern(
        [{"close": 100, "volume": Decimal("1000.125")} for _ in range(50)]
    )
    assert fractional["avg_volume_50d"] == Decimal("1000.125")
