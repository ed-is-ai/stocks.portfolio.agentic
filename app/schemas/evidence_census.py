"""Typed contract for the aggregate evidence census (#636).

One row per security, disjoint accounting totals, and overlapping fault
counts. The models are pure carriers: every fact in them was established
elsewhere (the published scan artifact, the market view's coverage, or a
Strategy's declared minimums) and is reported here verbatim.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

#: Fault vocabulary. Each name states *how* evidence is unusable, never why.
FAULT_THIN = "thin"
FAULT_GAPPED = "gapped"
FAULT_MISSING_FRAGMENT = "missing_fragment"
FAULT_DROPPED = "dropped"

EvidenceCensusNamespace = Literal["scan", "portfolio", "none"]
EvidenceCensusPath = Literal["entry", "exit"]


class _FrozenCensusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceCensusShortfallV1(_FrozenCensusModel):
    """One Strategy path whose declared session minimum is not met."""

    strategy: str = Field(min_length=1)
    path: EvidenceCensusPath
    available: int = Field(ge=0)
    required: int = Field(ge=0)


class EvidenceCensusSecurityV1(_FrozenCensusModel):
    """One security's accounted evidence state."""

    security_id: str = Field(min_length=1)
    display_ticker: str = ""
    namespace: EvidenceCensusNamespace
    in_universe: bool
    sessions: int = Field(default=0, ge=0)
    gap_reason: str | None = None
    gap_detail: str | None = None
    cause: str | None = None
    faults: tuple[str, ...] = ()
    shortfalls: tuple[EvidenceCensusShortfallV1, ...] = ()


class EvidenceCensusV1(_FrozenCensusModel):
    """The whole census: rows plus disjoint and overlapping totals.

    ``clean + faulted + dropped == total == len(securities)``, while
    ``sum(fault_counts.values())`` may exceed ``faulted`` because a single
    security can carry several independent faults.
    """

    as_of_session: date
    securities: tuple[EvidenceCensusSecurityV1, ...] = ()
    total: int = Field(default=0, ge=0)
    clean: int = Field(default=0, ge=0)
    faulted: int = Field(default=0, ge=0)
    dropped: int = Field(default=0, ge=0)
    fault_counts: Mapping[str, int] = {}
