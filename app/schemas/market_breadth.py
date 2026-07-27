"""S&P 500 market-breadth snapshot for the market-narrative layer (#109).

A single market-wide participation reading — the percentage of S&P 500 members
trading above their 200-day moving average — sourced from a public breadth
feed. Distinct from ``SectorAllocationSnapshot`` (which is computed from the
scan's *filtered* candidate list and therefore cannot describe the whole
market): breadth answers "how broad is participation across the index",
context the candidate list alone can't provide.
"""

from __future__ import annotations

from pydantic import BaseModel


class MarketBreadth(BaseModel):
    """Latest S&P 500 breadth reading (% of members above their 200DMA)."""

    pct_above_200dma: float  # 0..100
    smoothed_8ma: float | None = None  # 8-period MA of the breadth index (0..100)
    trend_rising: bool | None = None  # breadth's own 200MA trend is up
    bearish_signal: bool = False  # feed's bearish divergence flag
    as_of: str  # ISO date of the reading
    is_fresh: bool = True  # reading is recent enough to trust
    days_old: int | None = None  # calendar days between the reading and today
