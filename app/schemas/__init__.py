"""Schema layer — Pydantic models (not ORM).

Split by concern: ``scan`` (raw scan + scoring + analysis), ``record`` (the
composed scan+analysis record), and ``trade`` (portfolio/trading models).
"""

from app.schemas.alert import AlertSummary
from app.schemas.record import StockRecord
from app.schemas.scan import (
    CANSLIMScore,
    MomentumScore,
    StockAnalysis,
    StockScan,
)
from app.schemas.trade import EmailConfig, ExitSignal, Position, Trade
from app.schemas.pipeline_status import (
    PipelineStage,
    PipelineState,
    PipelineStatus,
    StageState,
    StageStatus,
)

__all__ = [
    "AlertSummary",
    "CANSLIMScore",
    "EmailConfig",
    "ExitSignal",
    "MomentumScore",
    "Position",
    "PipelineStage",
    "PipelineState",
    "PipelineStatus",
    "StockAnalysis",
    "StockRecord",
    "StockScan",
    "StageState",
    "StageStatus",
    "Trade",
]
