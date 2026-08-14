"""Schema layer — Pydantic models (not ORM).

Split by concern: ``scan`` (raw scan + scoring + analysis), ``record`` (the
composed scan+analysis record), and ``trade`` (portfolio/trading models).
"""

from app.schemas.alert import AlertSummary
from app.schemas.market_cycle import FomcMeeting, MarketCycleContext
from app.schemas.market_narrative import MarketNarrative, MarketNarrativeSource
from app.schemas.record import StockRecord
from app.schemas.scan import (
    CANSLIMScore,
    MomentumScore,
    StockAnalysis,
    StockScan,
)
from app.schemas.sector_allocation import (
    PortfolioSectorWeight,
    SectorAllocationSnapshot,
    SectorDelta,
    SectorShare,
)
from app.schemas.trade import (
    CashFlow,
    EmailConfig,
    ExitSignal,
    Portfolio,
    Position,
    SippImportResult,
    Trade,
)
from app.schemas.pipeline_status import (
    PipelineStage,
    PipelineState,
    PipelineStatus,
    StageState,
    StageStatus,
)
from app.schemas.realised_pnl import (
    RealisedPnlSummary,
    RoundTrip,
    SkippedInvalidDateTrade,
    UnmatchedSell,
)
from app.schemas.source_health import (
    SourceHealth,
    SourceName,
    SourceResult,
    SourceState,
)

__all__ = [
    "AlertSummary",
    "CANSLIMScore",
    "EmailConfig",
    "CashFlow",
    "ExitSignal",
    "FomcMeeting",
    "MarketCycleContext",
    "MarketNarrative",
    "MarketNarrativeSource",
    "MomentumScore",
    "Portfolio",
    "Position",
    "PortfolioSectorWeight",
    "PipelineStage",
    "PipelineState",
    "PipelineStatus",
    "RealisedPnlSummary",
    "RoundTrip",
    "SectorAllocationSnapshot",
    "SectorDelta",
    "SectorShare",
    "SkippedInvalidDateTrade",
    "StockAnalysis",
    "StockRecord",
    "StockScan",
    "SourceHealth",
    "SourceName",
    "SourceResult",
    "SourceState",
    "SippImportResult",
    "StageState",
    "StageStatus",
    "Trade",
    "UnmatchedSell",
]
