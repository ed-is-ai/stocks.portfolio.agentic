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
    strong_count: int = 0  # candidates in this sector scoring >= 7/10
    strong_share: float = 0.0  # fraction (0..1) of the whole universe that is
    # this sector AND high-conviction (>= 7/10)
    myb_count: int = 0  # candidates in this sector flagged multi-year breakout


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
    lookback_days: int | None = None  # gap to the delta baseline snapshot
    # (week-on-week ~= 7); None when there is no prior baseline to compare to


class PortfolioSectorWeight(BaseModel):
    """A sector's share of currently-held portfolio value (GBP-normalised)."""

    sector: str
    count: int  # number of held positions in this sector
    value_share: float  # fraction (0..1) of total portfolio GBP value


class CongressionalBuy(BaseModel):
    """A scanned name's net congressional/Senate buying over the last 12 months.

    ``congress_net`` and ``senate_net`` are buys minus sells (QuiverQuant
    transaction counts); only net-bought names are surfaced, so the narrative
    can call out which stocks lawmakers are accumulating.
    """

    ticker: str
    sector: str
    congress_net: int  # congress_buys - congress_sells
    senate_net: int  # senate_buys - senate_sells
