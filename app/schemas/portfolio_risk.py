"""Typed contract for the deterministic Portfolio Risk Coach (GH-16).

The models are pure carriers: every figure in them was computed by
``app.services.risk_engine.evaluate`` from one portfolio's positions, prices,
stops, sectors and GBP cash. Percentages are expressed in percent units
(``20`` means 20%) and money in GBP; GBP figures pass through the existing
float-based ``amount_in_gbp`` before being carried as ``Decimal``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

RiskSeverity = Literal["high", "medium", "info"]
RiskConfidence = Literal["complete", "limited"]
RiskFindingKind = Literal[
    "position_concentration",
    "sector_concentration",
    "unknown_sector",
    "capital_at_risk",
    "below_stop",
    "no_stop",
    "unpriced",
    "cash",
]


class _FrozenRiskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskPolicyV1(_FrozenRiskModel):
    """Fixed default limits, each a percentage of portfolio value."""

    max_position_pct: Decimal = Decimal("20")
    max_sector_pct: Decimal = Decimal("35")
    max_capital_at_risk_pct: Decimal = Decimal("6")


class RiskFindingV1(_FrozenRiskModel):
    """One risk observation plus the inputs that produced it."""

    kind: RiskFindingKind
    severity: RiskSeverity
    title: str
    detail: str
    tickers: tuple[str, ...] = ()
    #: Ordered ``(label, value)`` pairs, already formatted for display.
    inputs: tuple[tuple[str, str], ...] = ()
    action: str = ""


class RiskReportV1(_FrozenRiskModel):
    """A portfolio's risk findings, ordered risk-first, with their policy."""

    policy: RiskPolicyV1
    findings: tuple[RiskFindingV1, ...]
    confidence: RiskConfidence
    limitations: tuple[str, ...]
    #: Priced positions in GBP plus the GBP cash snapshot (0 when unknown).
    total_value_gbp: Decimal
