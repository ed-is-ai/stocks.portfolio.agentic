"""Strategy-assignment schemas (#440).

At most one Strategy governs a portfolio. These frozen models are the typed
seam shared by the repository, the assignment service, and the portfolio
templates — the recommendations screen (#441) and per-portfolio daily email
(#442) read the same shapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.services.backtest.strategy_protocol import JsonScalar

#: Freshness of the published analysis artifact the assignment's future
#: evaluations would read. ``stale`` means older than exactly 24 hours (the
#: 24h boundary itself is fresh); ``missing``/``unknown`` cover an absent or
#: unparseable artifact.
ScanFreshness = Literal["fresh", "stale", "missing", "unknown"]


class StrategyAssignment(BaseModel):
    """One persisted Strategy assignment for a portfolio.

    ``parameters`` is the canonical snapshot of the descriptor's validated
    default parameters at assignment time — never user-edited (V1 stores
    validated defaults only).
    """

    model_config = ConfigDict(frozen=True)

    portfolio_id: int
    strategy_id: str
    parameters: Mapping[str, JsonScalar]
    assigned_at: str
    updated_at: str


class AssignmentView(BaseModel):
    """A stored assignment joined against current Strategy discovery.

    An assignment whose ``strategy_id`` is no longer discoverable is
    retained with ``available=False`` — never silently dropped or switched.
    """

    model_config = ConfigDict(frozen=True)

    assignment: StrategyAssignment
    available: bool
    display_name: str | None
