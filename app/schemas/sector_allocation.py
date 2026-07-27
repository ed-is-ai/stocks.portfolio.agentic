"""Sector-allocation snapshot, run-over-run delta, and portfolio-weight schemas.

These are deterministic, LLM-independent structures: a scan-run's sector mix
(``SectorAllocationSnapshot``), how it shifted vs. the prior run
(``SectorDelta``), and how currently-held positions are distributed across
sectors (``PortfolioSectorWeight``). Phase 3's Claude summariser consumes the
same structures — only the narrative layer built on top of them changes.
"""

from __future__ import annotations

from pydantic import BaseModel


class SectorShare(BaseModel):
    """A sector's share of a scored population, by count and by score mass."""

    sector: str
    count: int
    count_share: float  # fraction (0..1) of total scored candidates
    score_share: float  # fraction (0..1) of total analyst-score mass


class SectorDelta(BaseModel):
    """Change in a sector's count-based share between two runs."""

    sector: str
    prior_share: float
    current_share: float
    delta: float  # current_share - prior_share


class SectorAllocationSnapshot(BaseModel):
    """Sector prevalence for one scan run, with optional run-over-run deltas.

    ``deltas`` is empty when there is no prior baseline to compare against
    (e.g. the first run ever, mirroring how ``scan_history`` treats empty
    history) — prevalence is still reported on its own.
    """

    as_of: str
    total_candidates: int
    shares: list[SectorShare] = []
    deltas: list[SectorDelta] = []


class PortfolioSectorWeight(BaseModel):
    """A sector's share of currently-held portfolio value (GBP-normalised)."""

    sector: str
    count: int  # number of held positions in this sector
    value_share: float  # fraction (0..1) of total portfolio GBP value
