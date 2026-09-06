"""Typed pipeline boundary for the portfolio price-evidence repair pass."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.agents.base import Agent
from app.services.snapshot_repair import SnapshotRepairReport, SnapshotRepairService


class PriceBackfillPayload(BaseModel):
    """Optional scope for one portfolio price-evidence repair pass."""

    portfolio_id: int | None = None


class PriceBackfillAgent(Agent):
    """Run the existing snapshot repair service as one pipeline stage."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    repair_service: SnapshotRepairService

    def run(self, payload: PriceBackfillPayload) -> SnapshotRepairReport:
        return self.repair_service.repair(portfolio_id=payload.portfolio_id)
