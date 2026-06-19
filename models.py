"""Backward-compatibility shim — models moved to ``app.schemas``.

Import from ``app.schemas`` (or its submodules) in new code. This module
re-exports the schema classes so existing ``from models import ...`` keeps
working during the layered-architecture migration.
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

__all__ = [
    "AlertSummary",
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
