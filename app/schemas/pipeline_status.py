"""Structured, persisted status for one pipeline run."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PipelineStage(StrEnum):
    SOURCES = "sources"
    MARKET_DATA = "market_data"
    ENRICHMENT = "enrichment"
    ANALYSIS = "analysis"
    ALERTS = "alerts"
    EXPORT = "export"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class StageState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    SKIPPED = "skipped"
    FAILED = "failed"


class PipelineState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self is not PipelineState.RUNNING


class StageStatus(BaseModel):
    stage: PipelineStage
    state: StageState = StageState.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    current: int | None = None
    total: int | None = None
    substage: str | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("pipeline timestamps must include a timezone")
        return value


class PipelineStatus(BaseModel):
    schema_version: int = 1
    run_id: str | None = None
    state: PipelineState = PipelineState.IDLE
    current_stage: PipelineStage | None = None
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    elapsed_seconds: float = 0.0
    stages: list[StageStatus] = Field(
        default_factory=lambda: [StageStatus(stage=stage) for stage in PipelineStage]
    )
    source_health: dict[str, object] = Field(default_factory=dict)
    scanned: int = 0
    analysed: int = 0
    actionable: int = 0
    error_summary: str | None = None

    @field_validator("started_at", "updated_at", "completed_at")
    @classmethod
    def require_aware_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("pipeline timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_stage_contract(self) -> Self:
        if [item.stage for item in self.stages] != list(PipelineStage):
            raise ValueError("pipeline stages must contain every stage in order")
        return self

    @classmethod
    def idle(cls) -> PipelineStatus:
        return cls()

    def stage(self, name: PipelineStage) -> StageStatus:
        return next(item for item in self.stages if item.stage is name)
