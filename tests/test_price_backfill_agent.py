"""Tests for the typed price-evidence pipeline agent (#492)."""

from __future__ import annotations

from unittest.mock import Mock

from app.agents.price_backfill.price_backfill_agent import (
    PriceBackfillAgent,
    PriceBackfillPayload,
)
from app.services.snapshot_repair import SnapshotRepairReport, SnapshotRepairService


def test_agent_runs_the_existing_repair_service_with_typed_scope(
    monkeypatch,
) -> None:
    service = SnapshotRepairService(Mock(), Mock())
    repair = Mock(
        return_value=SnapshotRepairReport(
            scanned=0,
            candidates=0,
            repaired=0,
            marked_unavailable=0,
            unchanged=0,
            dry_run=False,
        )
    )
    monkeypatch.setattr(service, "repair", repair)
    agent = PriceBackfillAgent(name="PriceBackfillAgent", repair_service=service)

    result = agent.run(PriceBackfillPayload(portfolio_id=7))

    repair.assert_called_once_with(portfolio_id=7)
    assert result.scanned == 0
