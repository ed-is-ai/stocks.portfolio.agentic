"""Typed, safe outcomes for external pipeline sources."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
import re

from pydantic import BaseModel, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SourceName(StrEnum):
    """Every external/local source whose coverage is tracked separately."""

    WHALE_WISDOM = "whale_wisdom"
    STOCKTWITS = "stocktwits"
    VCP_FMP = "vcp_fmp"
    TRADINGVIEW_US = "tradingview_us"
    TRADINGVIEW_UK = "tradingview_uk"
    YAHOO_MARKET_DATA = "yahoo_market_data"
    ALPHA_VANTAGE = "alpha_vantage"
    CONGRESS = "congress"

    @property
    def label(self) -> str:
        """Return the human-readable display name for this source."""
        return {
            self.WHALE_WISDOM: "WhaleWisdom",
            self.STOCKTWITS: "StockTwits",
            self.VCP_FMP: "VCP / FMP",
            self.TRADINGVIEW_US: "TradingView US",
            self.TRADINGVIEW_UK: "TradingView UK",
            self.YAHOO_MARKET_DATA: "Yahoo market data",
            self.ALPHA_VANTAGE: "Alpha Vantage",
            self.CONGRESS: "Congress",
        }[self]

    @property
    def stage(self) -> SourceStage:
        """Return the pipeline funnel stage this source belongs to."""
        return {
            self.WHALE_WISDOM: SourceStage.DISCOVERY,
            self.STOCKTWITS: SourceStage.DISCOVERY,
            self.VCP_FMP: SourceStage.DISCOVERY,
            self.TRADINGVIEW_US: SourceStage.DISCOVERY,
            self.TRADINGVIEW_UK: SourceStage.DISCOVERY,
            self.YAHOO_MARKET_DATA: SourceStage.MARKET_DATA,
            self.ALPHA_VANTAGE: SourceStage.ENRICHMENT,
            self.CONGRESS: SourceStage.ENRICHMENT,
        }[self]


class SourceStage(StrEnum):
    """Pipeline funnel stage a source belongs to, for grouped display."""

    DISCOVERY = "discovery"
    MARKET_DATA = "market_data"
    ENRICHMENT = "enrichment"

    @property
    def label(self) -> str:
        """Return the human-readable display name for this stage."""
        return {
            self.DISCOVERY: "Discovery",
            self.MARKET_DATA: "Market data",
            self.ENRICHMENT: "Enrichment",
        }[self]

    @property
    def order(self) -> int:
        """Return the display order for this stage in the pipeline funnel."""
        return {
            self.DISCOVERY: 0,
            self.MARKET_DATA: 1,
            self.ENRICHMENT: 2,
        }[self]


class SourceState(StrEnum):
    """Explicit, safe outcome states for one source in one pipeline run."""

    OK = "ok"
    EMPTY = "empty"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def label(self) -> str:
        """Return the title-cased display label for this state."""
        return self.value.title()


_SECRET_PATTERN = re.compile(
    r"(?i)(?:(?:api[_ -]?key|apikey)\s*[:=]\s*|"
    r"(?:password|secret|token)(?:\s*[:=]\s*|\s+))[^\s,;&]+"
)


def safe_display_message(value: str) -> str:
    """Return a short single-line message with credential-shaped values removed."""
    collapsed = " ".join(value.split())[:500]
    if _SECRET_PATTERN.search(collapsed):
        return "Provider request failed (credentials redacted)."
    return collapsed


class SourceHealth(BaseModel):
    """Structured, display-safe outcome for one source in one pipeline run."""

    source: SourceName
    state: SourceState
    count: int = Field(default=0, ge=0)
    detail_code: str = ""
    display_message: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0)

    @field_validator("display_message")
    @classmethod
    def redact_display_message(cls, value: str) -> str:
        """Strip credential-shaped substrings from the displayed message."""
        return safe_display_message(value)

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_timestamps(cls, value: datetime | None) -> datetime | None:
        """Reject naive timestamps so freshness math never silently misfires."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("source timestamps must include a timezone")
        return value

    @property
    def label(self) -> str:
        """Return the source's human-readable display name."""
        return self.source.label

    @property
    def state_label(self) -> str:
        """Return the state's human-readable display label."""
        return self.state.label

    @property
    def stage(self) -> SourceStage:
        """Return the pipeline funnel stage this source belongs to."""
        return self.source.stage

    @property
    def stage_label(self) -> str:
        """Return the stage's human-readable display label."""
        return self.source.stage.label

    @property
    def stage_order(self) -> int:
        """Return the stage's display order for stage-ordered rendering."""
        return self.source.stage.order


class SourceResult(BaseModel):
    """Ticker-oriented source response paired with its structured health."""

    items: list[str] = Field(default_factory=list)
    health: SourceHealth

    @property
    def tickers(self) -> list[str]:
        """Compatibility alias for the former TradingView result contract."""
        return self.items

    @property
    def status(self) -> str:
        """Compatibility alias exposing the raw health state value."""
        return self.health.state.value

    @property
    def detail(self) -> str:
        """Compatibility alias exposing the health display message."""
        return self.health.display_message

    @classmethod
    def from_items(
        cls,
        source: SourceName,
        items: list[str],
        *,
        started_at: datetime | None = None,
        display_message: str = "",
        detail_code: str = "",
    ) -> SourceResult:
        """Build a result for a source call that completed successfully."""
        completed = _utc_now()
        started = started_at or completed
        return cls(
            items=items,
            health=SourceHealth(
                source=source,
                state=SourceState.OK if items else SourceState.EMPTY,
                count=len(items),
                detail_code=detail_code,
                display_message=display_message,
                started_at=started,
                completed_at=completed,
                duration_seconds=max(0.0, (completed - started).total_seconds()),
            ),
        )

    @classmethod
    def unavailable(
        cls,
        source: SourceName,
        state: SourceState,
        detail_code: str,
        display_message: str,
        *,
        started_at: datetime | None = None,
    ) -> SourceResult:
        """Build a result for a source that was skipped or failed outright."""
        if state not in {SourceState.SKIPPED, SourceState.FAILED}:
            raise ValueError("unavailable results must be skipped or failed")
        completed = _utc_now()
        started = started_at or completed
        return cls(
            health=SourceHealth(
                source=source,
                state=state,
                detail_code=detail_code,
                display_message=display_message,
                started_at=started,
                completed_at=completed,
                duration_seconds=max(0.0, (completed - started).total_seconds()),
            )
        )


def derive_pipeline_outcome(
    source_health: Mapping[SourceName, SourceHealth],
    *,
    analysed: int,
    artifact_produced: bool,
):
    """Derive the terminal pipeline state from output usability and coverage."""
    from app.schemas.pipeline_status import PipelineState

    if analysed <= 0 or not artifact_produced:
        return PipelineState.FAILED
    if any(
        item.state in {SourceState.SKIPPED, SourceState.FAILED}
        for item in source_health.values()
    ):
        return PipelineState.PARTIAL
    return PipelineState.COMPLETE
