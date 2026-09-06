"""Weinstein stage detector runner."""

from app.core.stage_classification import classify_weinstein_stage
from app.core.technical_indicators import weekly_closes
from app.services.backtest.detector_contracts import (
    DetectorContext,
    DetectorExecutionError,
)
from app.services.backtest.historical_scan_record import (
    DetectorResultV1,
    StageResultV1,
    StageV1,
)


def run_stage(context: DetectorContext) -> DetectorResultV1:
    if context.technicals is None:
        raise DetectorExecutionError(
            "integrity_error",
            "weinstein_stage_v1",
            "technical detector output is required",
        )
    technicals = context.technicals
    stage = classify_weinstein_stage(
        price=technicals.price,
        sma150=technicals.sma150,
        sma200=technicals.sma200,
        price_history=weekly_closes(context.rows),
    )
    return StageResultV1(stage=StageV1.model_validate({"value": stage}))
