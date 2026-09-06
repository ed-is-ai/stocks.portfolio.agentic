"""Technical detector runner."""

from app.core.technical_indicators import compute_reconstruction_technicals
from app.services.backtest.detector_contracts import DetectorContext
from app.services.backtest.historical_scan_record import (
    DetectorResultV1,
    TechnicalResultV1,
)


def run_technicals(context: DetectorContext) -> DetectorResultV1:
    return TechnicalResultV1(technicals=compute_reconstruction_technicals(context.rows))
