"""Schema layer — Pydantic models (not ORM).

Split by concern: ``scan`` (raw scan + scoring + analysis), ``record`` (the
composed scan+analysis record), and ``trade`` (portfolio/trading models).
"""

from app.schemas.record import StockRecord
from app.schemas.scan import (
    CANSLIMScore,
    MomentumScore,
    StockAnalysis,
    StockScan,
)
from app.schemas.trade import EmailConfig, ExitSignal, Position, Trade

__all__ = [
    "CANSLIMScore",
    "EmailConfig",
    "ExitSignal",
    "MomentumScore",
    "Position",
    "StockAnalysis",
    "StockRecord",
    "StockScan",
    "Trade",
]
